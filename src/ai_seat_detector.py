"""핸드당 1회만 부르는 AI(OpenAI vision) 좌석 판별.

task.md "AI 하이브리드 도입" 지침: 딜러 버튼 색이 카드 뒷면과 거의 같아 로컬
픽셀 판별로는 계속 헷갈리는 문제(dealer_position.py의 known limitation)를,
매 프레임이 아니라 "새 핸드가 시작되고 홀카드가 보일 때" 딱 한 번만 AI에게
사진을 보여주고 물어보는 것으로 우회한다. 그 이후 한 핸드 동안은 다시 로컬
로직(아우라/밝기/OCR)만 쓴다 - AI는 그 핸드의 "고정 좌석 맵"을 한 번 세팅하는
용도일 뿐이다.

실패(키 없음/타임아웃/파싱 실패 등)하면 예외를 던지지 않고 그냥 None을 반환한다
- 호출한 쪽(recorder.py)이 "AI 응답이 없으면 기존 로컬 모드로" 그대로 진행하게
만들기 위해서다(안전한 폴백).
"""
import base64
import io
import json
import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

MODEL = os.environ.get("AI_SEAT_MODEL", "gpt-4o-mini")
TIMEOUT_SEC = float(os.environ.get("AI_SEAT_TIMEOUT_SEC", "4.0"))
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_call_log.txt")

SEAT_LAYOUT_DESC = (
    "이 6인 홀덤 테이블 스크린샷의 좌석 번호는 다음과 같이 고정되어 있다:\n"
    "0 = 상단좌, 1 = 상단중앙, 2 = 상단우, 3 = 하단우, 4 = 하단중앙, 5 = 하단좌\n"
    "(화면을 보는 사람 기준 방향이며, 시계방향 순서는 0->1->2->3->4->5->0 이다)"
)

PROMPT = (
    SEAT_LAYOUT_DESC + "\n\n"
    "이 이미지를 보고 아래 정보를 JSON으로만 답해라. 설명 문장 없이 JSON만 출력한다.\n"
    "dealer_seat/hero_seat/occupied_seats의 좌석 번호는 반드시 따옴표 없는 숫자(정수)로 써라 "
    '(예: 2, "2"라고 문자열로 쓰지 말 것).\n'
    "{\n"
    '  "dealer_seat": <딜러 버튼(D 표시)이 있는 좌석 번호(0~5, 정수), 못 찾으면 null>,\n'
    '  "hero_seat": <하단 중앙 근처, 자기 자신의 홀카드가 앞면으로 보이는 좌석 번호(정수), 안 보이면 null>,\n'
    '  "hero_cards": <히어로의 홀카드 2장을 "As Kh" 형식으로, 안 보이면 빈 문자열>,\n'
    '  "occupied_seats": [<지금 사람이 앉아있는 좌석 번호(정수) 목록(빈 자리/+ 버튼만 있는 자리는 제외)>],\n'
    '  "seat_positions": {<선택, 각 좌석 번호를 key로, 화면상 대략 위치를 짧게 설명>}\n'
    "}"
)


@dataclass
class AiSeatResult:
    dealer_seat: int | None
    hero_seat: int | None
    hero_cards: str
    occupied_seats: list[int]
    seat_positions: dict


def _log(reason: str, success: bool, latency_ms: float, detail: str = "") -> None:
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{reason}\t"
        f"{'OK' if success else 'FAIL'}\t{latency_ms:.0f}ms\t{detail}\n"
    )
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 로그 실패는 호출 자체를 막을 이유가 안 됨


def _image_to_data_url(img, max_side: int = 1024, quality: int = 82) -> str:
    """비용/속도를 위해 적당히 축소해서 JPEG로 인코딩한다(카드 숫자는 읽혀야
    하니 너무 작게 줄이지는 않는다)."""
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _parse_response(text: str, seat_count: int) -> AiSeatResult | None:
    if text is None:
        return None
    text = text.strip()
    # response_format=json_object 로 강제해도, 가끔 ```json ... ``` 코드펜스로
    # 감싸서 주는 경우가 실측(비슷한 다른 프로젝트 경험)상 있어서 방어적으로 벗긴다.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except Exception:
        return None

    def _to_seat_index(v):
        """딜러/히어로 좌석 번호가 정수(2)가 아니라 문자열("2")로 오는 경우가
        실측 확인됐다(모델이 가끔 숫자를 문자열로 반환) - 이 때문에 유효한 응답도
        계속 "AI 인식 실패"로 버려지고 있었다. 정수로 변환 가능하면 변환해서 받아준다."""
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            iv = v
        elif isinstance(v, float) and v.is_integer():
            iv = int(v)
        elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
            iv = int(v.strip())
        else:
            return None
        return iv if 0 <= iv < seat_count else None

    dealer_seat = _to_seat_index(data.get("dealer_seat"))
    hero_seat = _to_seat_index(data.get("hero_seat"))
    hero_cards = data.get("hero_cards") or ""
    if not isinstance(hero_cards, str):
        hero_cards = ""
    occupied = data.get("occupied_seats") or []
    if not isinstance(occupied, list):
        occupied = []
    occupied = [s for s in (_to_seat_index(x) for x in occupied) if s is not None]
    seat_positions = data.get("seat_positions") or {}
    if not isinstance(seat_positions, dict):
        seat_positions = {}

    return AiSeatResult(
        dealer_seat=dealer_seat, hero_seat=hero_seat, hero_cards=hero_cards.strip(),
        occupied_seats=occupied, seat_positions=seat_positions,
    )


def detect_seats_via_ai(img, seat_count: int = 6, reason: str = "hand_start") -> AiSeatResult | None:
    """실패하면(키 없음/타임아웃/파싱 실패 등) None을 반환한다 - 호출부가 로컬
    모드로 그대로 폴백하면 된다. 예외를 밖으로 던지지 않는다."""
    start = time.time()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _log(reason, False, 0, "OPENAI_API_KEY 없음")
        return None

    try:
        from openai import OpenAI
    except Exception as e:
        _log(reason, False, 0, f"openai 패키지 임포트 실패: {e}")
        return None

    try:
        client = OpenAI(api_key=api_key, timeout=TIMEOUT_SEC, max_retries=0)
        data_url = _image_to_data_url(img)
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=400,
            timeout=TIMEOUT_SEC,  # 클라이언트 생성 시점 timeout과 별개로, 호출별로도 명시(실측: 이렇게 둘 다 주는 편이 확실히 적용됨)
        )
        text = resp.choices[0].message.content
    except Exception as e:
        latency = (time.time() - start) * 1000
        _log(reason, False, latency, f"{type(e).__name__}: {e}")
        return None

    latency = (time.time() - start) * 1000
    result = _parse_response(text, seat_count)
    if result is None:
        _log(reason, False, latency, "JSON 파싱 실패")
        return None
    _log(reason, True, latency, f"dealer={result.dealer_seat} hero={result.hero_seat} cards={result.hero_cards!r}")
    return result
