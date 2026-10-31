"""下载并理解小红书笔记中的图片与视频（OpenAI 兼容视觉接口）。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from scripts.aggregate_results import plain_summary_text
from scripts.secure_credentials import read_secret, safe_child_environment, validate_credential_id

DEFAULT_LLM_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "endpoint_url": "",
    "model": "",
    "credential_id": "social-trend-llm",
    "auth_header": "Authorization",
    "auth_scheme": "Bearer",
    "audio_model": "",
    "max_images_per_note": 6,
    "max_video_frames": 6,
    "frame_interval_seconds": 3,
    "max_image_edge": 1024,
    "max_video_seconds": 300,
    "request_timeout_seconds": 120,
    "ffmpeg_bin": "",
}

RUN_FILE_SAFE = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
NOTE_ID_SAFE = re.compile(r"^[A-Fa-f0-9]{16,32}$")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_MEDIA_BYTES = 256 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 18 * 1024 * 1024
MAX_AUDIO_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _int_setting(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"LLM 设置 {name} 必须是 {minimum} 到 {maximum} 之间的整数。")
    return value


def normalize_llm_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("llm_settings.json 顶层必须是 JSON 对象。")
    secret_names = ("apikey", "token", "secret", "password", "authorization")
    if any(any(part in re.sub(r"[^a-z]", "", str(key).lower()) for part in secret_names) for key in value):
        raise ValueError("配置中不能包含 API 密钥或令牌值；请通过密钥管理命令写入系统凭据库。")

    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("LLM 设置 enabled 必须是布尔值。")
    text_limits = {"endpoint_url": 1200, "model": 200, "credential_id": 64, "auth_header": 100, "auth_scheme": 40, "audio_model": 200, "ffmpeg_bin": 1000}
    strings: dict[str, str] = {}
    for name, limit in text_limits.items():
        raw = value.get(name, DEFAULT_LLM_SETTINGS[name])
        if raw is None:
            raw = ""
        if not isinstance(raw, str) or len(raw) > limit:
            raise ValueError(f"LLM 设置 {name} 必须是 {limit} 字符以内的文本。")
        strings[name] = raw.strip()

    if strings["credential_id"]:
        validate_credential_id(strings["credential_id"])
    allowed_headers = {"authorization", "api-key", "x-api-key", "x-goog-api-key"}
    if strings["auth_header"].lower() not in allowed_headers:
        raise ValueError("认证请求头仅支持 Authorization、api-key、x-api-key 或 x-goog-api-key。")
    if strings["auth_scheme"] and not re.fullmatch(r"[A-Za-z0-9._-]+", strings["auth_scheme"]):
        raise ValueError("认证方案只能包含英文字母、数字、点、下划线和连字符。")

    endpoint_url = strings["endpoint_url"]
    if endpoint_url:
        parsed = urllib.parse.urlsplit(endpoint_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("LLM 端点必须是有效的 HTTP(S) URL，且不能把密钥写进 URL。")
        if parsed.query or parsed.fragment:
            raise ValueError("LLM 端点不能包含查询参数或片段；请勿把密钥放进 URL。")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and (parsed.hostname.lower() not in local_hosts or strings["credential_id"]):
            raise ValueError("远程 LLM 端点必须使用 HTTPS；如使用本机无认证服务，请清空密钥凭据标识。")
    if enabled and (not endpoint_url or not strings["model"]):
        raise ValueError("启用媒体分析前必须填写 LLM 端点和模型名称。")

    return {
        **strings,
        "enabled": enabled,
        "max_images_per_note": _int_setting(value.get("max_images_per_note", DEFAULT_LLM_SETTINGS["max_images_per_note"]), "max_images_per_note", 1, 12),
        "max_video_frames": _int_setting(value.get("max_video_frames", DEFAULT_LLM_SETTINGS["max_video_frames"]), "max_video_frames", 1, 12),
        "frame_interval_seconds": _int_setting(value.get("frame_interval_seconds", DEFAULT_LLM_SETTINGS["frame_interval_seconds"]), "frame_interval_seconds", 1, 60),
        "max_image_edge": _int_setting(value.get("max_image_edge", DEFAULT_LLM_SETTINGS["max_image_edge"]), "max_image_edge", 384, 2048),
        "max_video_seconds": _int_setting(value.get("max_video_seconds", DEFAULT_LLM_SETTINGS["max_video_seconds"]), "max_video_seconds", 10, 1800),
        "request_timeout_seconds": _int_setting(value.get("request_timeout_seconds", DEFAULT_LLM_SETTINGS["request_timeout_seconds"]), "request_timeout_seconds", 10, 600),
    }


def get_configured_secret(settings: dict[str, Any]) -> str:
    credential_id = settings.get("credential_id", "")
    if not credential_id:
        return ""
    secret = read_secret(credential_id)
    if not secret:
        raise ValueError(f"密钥凭据“{credential_id}”尚未配置；请在终端运行密钥管理命令。")
    return secret


def _endpoint(settings: dict[str, Any], path: str) -> str:
    base = settings["endpoint_url"].rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/audio/transcriptions"):
        base = base[: -len("/audio/transcriptions")]
    return base + path


def _http_get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers}, method="GET")
    opener = urllib.request.build_opener(_RejectRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("模型列表响应超过 5 MB 限制。")
            parsed = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError(f"获取模型列表时认证失败（HTTP {exc.code}），请检查密钥凭据。") from None
        if exc.code in {404, 405}:
            raise ValueError("端点不支持兼容的 /models 接口，可手动填写模型名称。") from None
        raise ValueError(f"模型列表端点返回 HTTP {exc.code}。") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接模型列表端点：{exc.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("模型列表端点没有返回有效 JSON。") from None
    if not isinstance(parsed, dict):
        raise ValueError("模型列表响应格式不正确。")
    return parsed


def get_available_models(raw_settings: Any) -> list[str]:
    settings = normalize_llm_settings(raw_settings)
    if not settings["endpoint_url"]:
        raise ValueError("请先填写 API 端点。")
    secret = get_configured_secret(settings)
    response = _http_get_json(
        _endpoint(settings, "/models"),
        _authorization(settings, secret),
        settings["request_timeout_seconds"],
    )
    entries = response.get("data")
    if not isinstance(entries, list):
        entries = response.get("models")
    if not isinstance(entries, list):
        raise ValueError("模型列表响应中没有兼容的 data 或 models 数组；可手动填写模型名称。")
    secret_variants = {secret, urllib.parse.quote(secret, safe="")} if secret else set()
    model_ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            model_id = entry.strip()
        elif isinstance(entry, dict):
            model_id = str(entry.get("id") or entry.get("name") or entry.get("model") or "").strip()
        else:
            continue
        if model_id and not any(token and token in model_id for token in secret_variants) and len(model_id) <= 200 and model_id not in seen:
            seen.add(model_id)
            model_ids.append(model_id)
        if len(model_ids) >= 500:
            break
    if not model_ids:
        raise ValueError("端点未返回可用模型；可手动填写模型名称。")
    return model_ids


def _authorization(settings: dict[str, Any], secret: str) -> dict[str, str]:
    if not secret:
        return {}
    scheme = settings["auth_scheme"]
    value = f"{scheme} {secret}" if scheme else secret
    return {settings["auth_header"]: value}


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _http_json(url: str, body: bytes, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_RejectRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                raise ValueError("LLM 响应超过 5 MB 限制。")
            parsed = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError(f"LLM 端点认证失败（HTTP {exc.code}），请检查密钥标识、密钥权限和认证请求头。") from None
        raise ValueError(f"LLM 端点返回 HTTP {exc.code}。") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接 LLM 端点：{exc.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("LLM 端点没有返回有效 JSON。") from None
    if not isinstance(parsed, dict):
        raise ValueError("LLM 端点返回的数据格式不正确。")
    return parsed


def chat_completion(
    settings: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 2400,
) -> str:
    secret = get_configured_secret(settings)
    endpoint_host = (urllib.parse.urlsplit(settings["endpoint_url"]).hostname or "").lower()
    is_mimo = endpoint_host == "api.xiaomimimo.com" or endpoint_host.endswith(".xiaomimimo.com")
    payload: dict[str, Any] = {"model": settings["model"], "messages": messages, "temperature": 0}
    if is_mimo:
        payload["max_completion_tokens"] = max_tokens
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["max_tokens"] = max_tokens
    headers = {"Content-Type": "application/json", "Accept": "application/json", **_authorization(settings, secret)}
    response = _http_json(
        _endpoint(settings, "/chat/completions"),
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers,
        settings["request_timeout_seconds"],
    )
    try:
        choice = response["choices"][0]
        message = choice["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("LLM 响应中缺少 choices[0].message.content。") from None
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in content
            if isinstance(item, dict)
        )
    if not isinstance(content, str) or not content.strip():
        finish_reason = _text(choice.get("finish_reason")) or "未知"
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        reasoning_tokens = details.get("reasoning_tokens")
        reasoning_present = bool(_text(message.get("reasoning_content")))
        diagnostic = [f"finish_reason={finish_reason}"]
        if isinstance(reasoning_tokens, int):
            diagnostic.append(f"reasoning_tokens={reasoning_tokens}")
        if reasoning_present:
            diagnostic.append("仅返回了 reasoning_content")
        raise ValueError(f"LLM 返回了空内容（{'，'.join(diagnostic)}）。")
    return content.strip()


def test_llm_connection(raw_settings: Any) -> dict[str, Any]:
    settings = normalize_llm_settings(raw_settings)
    if not settings["endpoint_url"] or not settings["model"]:
        raise ValueError("请先填写 LLM 端点和模型名称。")
    try:
        result = chat_completion(
            settings,
            [
                {"role": "system", "content": "你正在进行连接检查。只回复 OK。"},
                {"role": "user", "content": "OK"},
            ],
            max_tokens=8,
        )
    except Exception as exc:
        raise ValueError(_safe_error(exc, settings)) from None
    return {"connected": True, "model": settings["model"], "response_received": bool(result)}


def _ffmpeg(settings: dict[str, Any]) -> str:
    configured = settings.get("ffmpeg_bin", "").strip()
    executable = shutil.which(configured) if configured else shutil.which("ffmpeg")
    if not executable and configured and Path(configured).is_file():
        executable = configured
    if not executable:
        raise ValueError("找不到 FFmpeg；请安装 FFmpeg 并加入 PATH，或在 WebUI 填写 ffmpeg.exe 路径。")
    return executable


def _run_ffmpeg(command: list[str], timeout: int = 240) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, env=safe_child_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"FFmpeg 处理失败：{type(exc).__name__}。") from None
    if result.returncode != 0:
        raise ValueError(f"FFmpeg 处理失败（退出码 {result.returncode}）。")


def _normalize_image(source: Path, destination: Path, settings: dict[str, Any], ffmpeg: str | None) -> Path:
    if ffmpeg:
        _run_ffmpeg([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-frames:v", "1", "-vf", f"scale={settings['max_image_edge']}:{settings['max_image_edge']}:force_original_aspect_ratio=decrease",
            "-q:v", "5", str(destination),
        ])
        if destination.is_file() and destination.stat().st_size <= MAX_IMAGE_BYTES:
            return destination
    if source.stat().st_size <= MAX_IMAGE_BYTES and source.suffix.lower() in IMAGE_EXTENSIONS:
        return source
    raise ValueError("图片无法安全缩放到单张 5 MB 以内。")


def _format_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _extract_video_frames(video: Path, target_dir: Path, settings: dict[str, Any], ffmpeg: str, limit: int) -> list[tuple[Path, int]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = target_dir / "frame-%02d.jpg"
    filter_value = f"fps=1/{settings['frame_interval_seconds']},scale={settings['max_image_edge']}:{settings['max_image_edge']}:force_original_aspect_ratio=decrease"
    _run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-t", str(settings["max_video_seconds"]), "-vf", filter_value, "-frames:v", str(limit),
        "-q:v", "5", str(output_pattern),
    ])
    frames = sorted(target_dir.glob("frame-*.jpg"))
    interval = settings["frame_interval_seconds"]
    return [(path, index * interval) for index, path in enumerate(frames)]


def _transcribe_audio(video: Path, temp_dir: Path, settings: dict[str, Any], ffmpeg: str, secret: str) -> str:
    model = settings.get("audio_model", "").strip()
    if not model:
        return ""
    temp_dir.mkdir(parents=True, exist_ok=True)
    audio_path = temp_dir / "audio.mp3"
    _run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "-t", str(settings["max_video_seconds"]), str(audio_path),
    ])
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        return ""
    if audio_path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("转录音频超过 24 MB 上限。")
    boundary = "----socialtrend" + os.urandom(16).hex()
    chunks: list[bytes] = []
    def add_field(name: str, value: str) -> None:
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"])
    add_field("model", model)
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n',
        b"Content-Type: audio/mpeg\r\n\r\n",
        audio_path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json", **_authorization(settings, secret)}
    response = _http_json(_endpoint(settings, "/audio/transcriptions"), b"".join(chunks), headers, settings["request_timeout_seconds"])
    text = response.get("text")
    return text.strip() if isinstance(text, str) else ""


def _decode_model_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return {"summary": text.strip(), "raw_text": text.strip()}
    if isinstance(value, dict):
        return value
    return {"summary": text.strip(), "raw_text": text.strip()}


def _secret_variants(settings: dict[str, Any]) -> set[str]:
    credential_id = settings.get("credential_id", "")
    if not credential_id:
        return set()
    try:
        secret = read_secret(credential_id)
    except Exception:
        secret = None
    if not secret:
        return set()
    return {secret, urllib.parse.quote(secret, safe="")}


def _redact_value(value: Any, settings: dict[str, Any]) -> Any:
    variants = _secret_variants(settings)

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            for secret in variants:
                if secret:
                    item = item.replace(secret, "[密钥已隐藏]")
            return item
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        return item

    return visit(value)


def _safe_error(error: BaseException, settings: dict[str, Any]) -> str:
    message = _redact_value(f"{type(error).__name__}: {error}", settings)
    return str(message)[:600]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _asset_kind(path: Path) -> str | None:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None


def _note_directory(run_id: str, note: dict[str, Any], index: int) -> str:
    safe_run_id = run_id if RUN_FILE_SAFE.fullmatch(run_id) else "run"
    note_id = _text(note.get("note_id"))
    source_item = note.get("source_item") if isinstance(note.get("source_item"), dict) else {}
    source_url = _text(note.get("url") or source_item.get("url"))
    suffix = note_id.lower() if NOTE_ID_SAFE.fullmatch(note_id) else hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return f"{safe_run_id}/note_{index:03d}_{suffix}"


def _collect_notes(run_data: dict[str, Any]):
    for collection_name in ("keyword_searches", "user_searches"):
        for entry in run_data.get(collection_name, []):
            if not isinstance(entry, dict):
                continue
            source_type = "keyword" if collection_name == "keyword_searches" else "user"
            for note in entry.get("notes", []):
                if isinstance(note, dict):
                    context = {
                        "source_type": source_type,
                        "topic": _text(entry.get("keyword")) if source_type == "keyword" else "",
                        "account": _text(entry.get("name")) if source_type == "user" else "",
                    }
                    yield note, context


def analyze_note_media(
    note: dict[str, Any],
    context: dict[str, str],
    output_dir: Path,
    run_data: dict[str, Any],
    index: int,
    settings: dict[str, Any],
    invoke_opencli: Callable[[list[str]], dict[str, Any]],
) -> dict[str, Any]:
    source_item = note.get("source_item") if isinstance(note.get("source_item"), dict) else {}
    note_url = _text(note.get("url") or source_item.get("url"))
    if not note_url:
        return {"status": "skipped", "error": "缺少笔记链接。", "assets": []}

    relative_dir = _note_directory(run_data["run_id"], note, index)
    media_dir = output_dir / "media" / relative_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    download = invoke_opencli([
        "xiaohongshu", "download", note_url,
        "--output", str(media_dir),
        "--window", "background",
        "--site-session", "ephemeral",
        "--keep-tab", "false",
        "-f", "json",
    ])
    command_info = {"status": download.get("status", "failed"), "exit_code": download.get("exit_code")}
    run_data.setdefault("media_commands", []).append(command_info)
    if download.get("status") != "success":
        run_data.setdefault("warnings", []).append({"stage": "media_download", "source": context.get("topic") or context.get("account"), "message": f"第 {index} 篇笔记媒体下载失败。"})
        return {"status": "download_failed", "download_status": download.get("status", "failed"), "error": "OpenCLI 媒体下载失败。", "assets": []}

    media_root = media_dir.resolve()
    assets = []
    for path in media_dir.rglob("*"):
        if path.is_symlink() or not path.is_file() or not _asset_kind(path):
            continue
        try:
            path.resolve().relative_to(media_root)
        except ValueError:
            continue
        assets.append(path)
    assets.sort(key=lambda path: (path.suffix.lower() in VIDEO_EXTENSIONS, path.name.lower()))
    if not assets:
        return {"status": "no_media", "download_status": "success", "error": "OpenCLI 未保存图片或视频文件。", "assets": []}

    asset_records = [
        {"type": _asset_kind(path), "path": path.relative_to(output_dir).as_posix(), "size_bytes": path.stat().st_size}
        for path in assets
    ]
    oversized = [path for path in assets if path.stat().st_size > MAX_SOURCE_MEDIA_BYTES]
    image_sources = [path for path in assets if _asset_kind(path) == "image" and path not in oversized][: settings["max_images_per_note"]]
    video_sources = [path for path in assets if _asset_kind(path) == "video" and path not in oversized]
    analysis_result: dict[str, Any] = {
        "status": "partial",
        "download_status": "success",
        "assets": asset_records,
        "summary": "",
        "visible_text": [],
        "key_moments": [],
        "audio_transcript": "",
        "error": "存在超过 256 MB 的媒体文件，已跳过这些文件。" if oversized else None,
    }
    frame_interval = settings["frame_interval_seconds"]
    try:
        ffmpeg = _ffmpeg(settings) if video_sources else shutil.which(settings.get("ffmpeg_bin") or "ffmpeg")
        visual_inputs: list[tuple[str, Path, int | None]] = []
        with tempfile.TemporaryDirectory(prefix="social-trend-media-") as temporary:
            temp_dir = Path(temporary)
            for image_index, image in enumerate(image_sources, start=1):
                destination = temp_dir / f"image-{image_index:02d}.jpg"
                try:
                    ready_image = _normalize_image(image, destination, settings, ffmpeg)
                except ValueError:
                    if image.stat().st_size <= MAX_IMAGE_BYTES:
                        ready_image = image
                    else:
                        continue
                visual_inputs.append((f"图片 {image_index}", ready_image, None))

            remaining_frames = settings["max_video_frames"]
            transcripts: list[str] = []
            secret = get_configured_secret(settings)
            for video_index, video in enumerate(video_sources, start=1):
                if not ffmpeg:
                    raise ValueError("找不到 FFmpeg，无法提取视频关键帧。")
                if remaining_frames > 0:
                    extracted = _extract_video_frames(video, temp_dir / f"video-{video_index:02d}", settings, ffmpeg, remaining_frames)
                    for frame_path, seconds in extracted:
                        visual_inputs.append((f"视频 {video_index} 关键帧 {_format_time(seconds)}", frame_path, seconds))
                    remaining_frames -= len(extracted)
                if settings.get("audio_model"):
                    try:
                        transcript = _redact_value(_transcribe_audio(video, temp_dir / f"audio-{video_index:02d}", settings, ffmpeg, secret), settings)
                    except ValueError as exc:
                        transcript = ""
                        analysis_result["error"] = _safe_error(exc, settings)
                        run_data.setdefault("warnings", []).append({"stage": "media_transcription", "source": context.get("topic") or context.get("account"), "message": f"第 {index} 篇视频音频转写失败。"})
                    if transcript:
                        transcripts.append(f"视频 {video_index}：{transcript}")
            analysis_result["audio_transcript"] = "\n".join(transcripts)
            if not visual_inputs:
                analysis_result["status"] = "analysis_failed"
                analysis_result["error"] = analysis_result["error"] or "没有可提交给视觉模型的有效图片或视频帧。"
                run_data.setdefault("warnings", []).append({"stage": "media_analysis", "source": context.get("topic") or context.get("account"), "message": f"第 {index} 篇笔记没有可分析的画面。"})
                return analysis_result

            source_item = note.get("source_item") if isinstance(note.get("source_item"), dict) else {}
            public_record = note.get("public_detail") if isinstance(note.get("public_detail"), dict) else {}
            body_fields = public_record.get("return_value")
            detail_values: dict[str, Any] = {}
            if isinstance(body_fields, list):
                detail_values = {
                    str(field.get("field")): field.get("value")
                    for field in body_fields
                    if isinstance(field, dict) and field.get("field") is not None
                }
            elif isinstance(body_fields, dict):
                detail_values = body_fields
            body = _text(detail_values.get("content") or detail_values.get("desc") or detail_values.get("description"))
            body = body or _text(source_item.get("content") or source_item.get("desc"))
            title = _text(source_item.get("title") or detail_values.get("title"))
            tags = _text(source_item.get("tags") or detail_values.get("tags"))
            prompt_context = {
                "笔记标题": title,
                "作者": _text(source_item.get("author")),
                "采集主题": context.get("topic", ""),
                "监控账号": context.get("account", ""),
                "笔记正文": body[:6000],
                "标签": tags,
                "视频音频转录": analysis_result["audio_transcript"][:12000],
            }
            prompt = (
                "请分析这篇小红书笔记的图片或视频关键帧。只描述画面中可见的事实；将文字识别、画面观察和推断分开。"
                "不清晰的招牌、地点、品牌或人物身份必须写为无法确认，不能猜测。视频帧标签含时间点，请据此整理关键时刻。"
                "结合笔记正文但不要把正文中的主张当作已验证事实。请只返回有效 JSON，字段为："
                "summary（简洁中文摘要），visible_text（画面可读文字数组），observed_elements（观察到的对象/场景数组），"
                "key_moments（包含 timestamp 和 description 的视频关键点数组），uncertainties（无法确认事项数组）。\n"
                "笔记上下文：\n" + json.dumps(prompt_context, ensure_ascii=False)
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            image_payload_bytes = 0
            for label, image_path, _seconds in visual_inputs:
                image_size = image_path.stat().st_size
                if image_payload_bytes + image_size > MAX_TOTAL_IMAGE_BYTES:
                    analysis_result["error"] = "为控制请求大小，部分图片或关键帧未发送给模型。"
                    continue
                image_payload_bytes += image_size
                content.append({"type": "text", "text": label})
                mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
                encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            raw_result = _redact_value(chat_completion(
                settings,
                [
                    {"role": "system", "content": "你是谨慎的图像与视频内容分析器。严格遵守用户指定的 JSON 字段和证据边界。"},
                    {"role": "user", "content": content},
                ],
            ), settings)
            parsed = _redact_value(_decode_model_json(raw_result), settings)
            analysis_result["status"] = "success" if not analysis_result["error"] else "partial"
            analysis_result["summary"] = plain_summary_text(parsed.get("summary") or parsed.get("overall_summary") or raw_result)
            analysis_result["visible_text"] = parsed.get("visible_text", []) if isinstance(parsed.get("visible_text", []), list) else [_text(parsed.get("visible_text"))]
            analysis_result["observed_elements"] = parsed.get("observed_elements", [])
            analysis_result["key_moments"] = parsed.get("key_moments", [])
            analysis_result["uncertainties"] = parsed.get("uncertainties", [])
    except (OSError, ValueError, RuntimeError) as exc:
        analysis_result["status"] = "analysis_failed"
        analysis_result["error"] = _safe_error(exc, settings)
        run_data.setdefault("warnings", []).append({"stage": "media_analysis", "source": context.get("topic") or context.get("account"), "message": f"第 {index} 篇笔记媒体分析失败：{exc}"})
    return analysis_result


def _media_identity(note: dict[str, Any], fallback_index: int) -> str:
    note_id = _text(note.get("note_id"))
    if NOTE_ID_SAFE.fullmatch(note_id):
        return f"id:{note_id.lower()}"
    source_item = note.get("source_item") if isinstance(note.get("source_item"), dict) else {}
    url = _text(note.get("url") or source_item.get("url"))
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc and parsed.path:
            return f"url:{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
    except ValueError:
        pass
    return f"row:{fallback_index}"


def process_media_for_run(
    run_data: dict[str, Any],
    output_dir: Path,
    settings: dict[str, Any],
    invoke_opencli: Callable[[list[str]], dict[str, Any]],
) -> None:
    if not settings.get("enabled"):
        return
    cached: dict[str, dict[str, Any]] = {}
    analyzed_count = 0
    for row_index, (note, context) in enumerate(_collect_notes(run_data), start=1):
        identity = _media_identity(note, row_index)
        if identity in cached:
            note["media_analysis"] = json.loads(json.dumps(cached[identity], ensure_ascii=False))
            continue
        analyzed_count += 1
        try:
            result = analyze_note_media(note, context, output_dir, run_data, analyzed_count, settings, invoke_opencli)
        except Exception as exc:
            result = {"status": "failed", "assets": [], "error": _safe_error(exc, settings)}
            run_data.setdefault("warnings", []).append({"stage": "media_analysis", "source": context.get("topic") or context.get("account"), "message": f"第 {analyzed_count} 篇笔记媒体处理失败。"})
        note["media_analysis"] = result
        cached[identity] = result
