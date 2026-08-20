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


def recognize_lines(img: Image.Image, lang: str = "ko", scale: float = 4.0) -> list[OcrLine]:
    """이미지에서 텍스트 줄 목록을 인식해서 반환한다 (세로 위치 순 정렬).

    좌표는 항상 원본 img 기준으로 정규화해서 반환한다 (scale 배율과 무관하게 일정).

    scale 기본값은 2.0 -> 4.0 으로 올렸다 (2026-08-19, 실제 코인포커 캡처로 실측).
    실제 좌석 크롭 크기(가로 150px 안팎)에서 2배는 "체크"/"호출" 태그를 놓쳤는데
    4배는 둘 다 정확히 잡았고, 이미 잘 되던 케이스(예: "올인")는 그대로였다 -
    회귀 없이 실제 효과가 있는 조정이었다. 대비 강화/이진화는 같은 크롭으로
    테스트했을 때 오히려 더 나쁘거나 차이가 없어서 적용하지 않았다.
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
