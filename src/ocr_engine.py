"""Windows 내장 OCR(winocr)을 이용한 텍스트 인식.

Tesseract 같은 별도 프로그램 설치 없이, Windows에 내장된 OCR 엔진을 사용한다.
(Windows 설정 > 시간 및 언어 > 언어 및 지역 에서 한국어 OCR 기능이 설치되어 있어야 한다.)
"""
from dataclasses import dataclass

import winocr
from PIL import Image


@dataclass
class OcrLine:
    text: str
    x: float  # 줄의 가로 위치 (원본 이미지 좌표 기준)
    y: float  # 줄의 세로 위치 (원본 이미지 좌표 기준)


def is_language_available(lang: str = "ko") -> bool:
    try:
        tags = [l.language_tag for l in winocr.OcrEngine.available_recognizer_languages]
        return any(t.lower().startswith(lang.lower()) for t in tags)
    except Exception:
        return False


def recognize_lines(img: Image.Image, lang: str = "ko", scale: float = 2.0) -> list[OcrLine]:
    """이미지에서 텍스트 줄 목록을 인식해서 반환한다 (세로 위치 순 정렬).

    좌표는 항상 원본 img 기준으로 정규화해서 반환한다 (scale 배율과 무관하게 일정).
    """
    if img is None:
        return []

    work_img = img
    effective_scale = 1.0
    if scale and scale != 1.0:
        try:
            w, h = img.size
            work_img = img.convert("L").resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
            )
            effective_scale = scale
        except Exception:
            work_img = img

    try:
        result = winocr.recognize_pil_sync(work_img, lang=lang)
    except Exception:
        return []

    lines = []
    for line in result.get("lines", []):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        words = line.get("words") or []
        if words:
            rect = words[0]["bounding_rect"]
            x = rect["x"] / effective_scale
            y = rect["y"] / effective_scale
        else:
            x = y = 0.0
        lines.append(OcrLine(text=text, x=x, y=y))

    lines.sort(key=lambda l: (l.y, l.x))
    return lines
