"""보드 카드 / 히어로 홀카드 AI 비전 인식 (task.md "히어로 매칭 확정 + 카드 인식(AI)").

카드 랭크 폰트가 장식체라 OCR로는 못 읽는 게 이미 확인돼서(board_reader.py 설명
참고) 카드 "값"은 AI 비전(OpenAI)으로만 읽는다. 카드 "장수" 자체는 여전히 저렴한
로컬 로직(board_reader.detect_card_count, 픽셀 밝기 기반)으로 판별하고, AI는
호출부(dealer_chat_bridge.py)가 "새 핸드 시작"/"스트리트 전환" 같은 딱 필요한
시점에만 핸드당 최소 횟수로 부른다(과도한 호출 금지 - task.md 3절).

기존 ai_seat_detector.py와 같은 관례를 따른다: .env의 OPENAI_API_KEY 사용,
ai_call_log.txt에 호출 로그 기록, 실패해도 예외를 던지지 않고 빈 문자열을
반환한다(호출하는 쪽이 행동 기록을 계속 진행하게 하기 위해서 - task.md
"AI 호출 실패 시에도 행동 기록은 계속되어야 함").
"""
import base64
import io
import json
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()

MODEL = os.environ.get("AI_CARD_MODEL", "gpt-4o-mini")
TIMEOUT_SEC = float(os.environ.get("AI_CARD_TIMEOUT_SEC", "6.0"))
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_call_log.txt")

_CARD_TOKEN_RE = re.compile(r"^[2-9TJQKA][SHDC]$", re.I)

HOLE_CARDS_PROMPT = (
    "이 이미지는 홀덤 테이블에서 한 플레이어의 홀카드(개인 카드 2장) 영역을 잘라낸 것이다.\n"
    "카드 앞면이 실제로 보이면 랭크+무늬로 읽어라(랭크: 2-9,T,J,Q,K,A / 무늬: s,h,d,c).\n"
    "카드 뒷면만 보이거나, 빈 공간이거나(관전 모드라 아무 카드도 없음), 확신이 없으면 "
    "억지로 추측하지 말고 빈 배열로 답하라.\n"
    '설명 없이 JSON만 답하라: {"cards": ["Ah", "Kd"]} 형식, 안 보이면 {"cards": []}'
)


def _board_prompt(expected_count: int, known_cards: list[str] | None = None) -> str:
    if known_cards:
        # task.md "보드 카드 재확인 흔들림 수정": 스트리트마다 보드 전체를 매번
        # 새로 읽으면, 이미 확정된 카드까지 다시 읽히면서 같은 카드가 스트리트별로
        # 다른 무늬/랭크로 보이는 흔들림이 실측 확인됐다(예: 플랍에서 Qd였던 카드가
        # 턴/리버에선 Qh로 읽힘). 이미 확인된 카드는 고정값으로 알려주고, AI에게는
        # "새로 추가된 카드만" 읽게 해서 이미 맞은 카드를 다시 흔들 여지 자체를 없앤다.
        new_count = expected_count - len(known_cards)
        known_str = " ".join(known_cards)
        return (
            f"이 이미지는 홀덤 테이블의 보드(커뮤니티) 카드 영역을 잘라낸 것이다. "
            f"왼쪽부터 {len(known_cards)}장은 이미 확인된 카드로 {known_str}이다 - 이 카드들은 "
            f"다시 읽지 않아도 된다(정답으로 그대로 쓸 것이다). 그 뒤에 새로 추가된 카드 "
            f"{new_count}장만 왼쪽부터 순서대로 랭크+무늬로 읽어라(랭크: 2-9,T,J,Q,K,A / "
            "무늬: s,h,d,c). 확신이 안 서는 카드는 억지로 추측하지 말고 빼라.\n"
            '설명 없이 JSON만 답하라: {"cards": ["7h", "2c"]} 형식(이미 확인된 카드는 '
            "포함하지 말고, 새로 추가된 카드만)."
        )
    return (
        f"이 이미지는 홀덤 테이블의 보드(커뮤니티) 카드 영역을 잘라낸 것이다. 지금 이 "
        f"시점에는 카드가 {expected_count}장 깔려 있어야 한다.\n"
        "왼쪽부터 순서대로 랭크+무늬로 읽어라(랭크: 2-9,T,J,Q,K,A / 무늬: s,h,d,c).\n"
        "확신이 안 서는 카드는 억지로 추측하지 말고 빼라(전체를 빈 배열로 남겨도 된다).\n"
        '설명 없이 JSON만 답하라: {"cards": ["As", "7h", "2c"]} 형식.'
    )


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


def _image_to_data_url(img, max_side: int = 640, quality: int = 88) -> str:
    """카드 영역만 잘라서 보내므로 원본이 이미 작다 - 랭크/무늬가 흐려지지 않게
    과하게 줄이지 않는다(홀카드/보드 크롭 기준 640px면 충분히 크다)."""
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _call_vision_json(img, prompt: str, reason: str) -> tuple[dict | None, float, str]:
    """실패하면 (None, 지연시간ms, 실패사유)를 반환한다 - 로그는 호출부가 남긴다
    (홀카드/보드마다 성공 시 detail이 다르므로 성공 로그는 각자 남기게 분리했다)."""
    start = time.time()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, 0.0, "OPENAI_API_KEY 없음"

    try:
        from openai import OpenAI
    except Exception as e:
        return None, 0.0, f"openai 패키지 임포트 실패: {e}"

    try:
        client = OpenAI(api_key=api_key, timeout=TIMEOUT_SEC, max_retries=0)
        data_url = _image_to_data_url(img)
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=150,
            timeout=TIMEOUT_SEC,
        )
        text = resp.choices[0].message.content
    except Exception as e:
        return None, (time.time() - start) * 1000, f"{type(e).__name__}: {e}"

    latency = (time.time() - start) * 1000
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except Exception:
        return None, latency, "JSON 파싱 실패"
    if not isinstance(data, dict):
        return None, latency, "JSON이 객체가 아님"
    return data, latency, ""


def _format_cards(raw_cards, max_count: int) -> str:
    if not isinstance(raw_cards, list):
        return ""
    valid = []
    for c in raw_cards:
        if not isinstance(c, str):
            continue
        c = c.strip()
        if _CARD_TOKEN_RE.match(c):
            valid.append(c[0].upper() + c[1].lower())
        if len(valid) >= max_count:
            break
    return " ".join(valid)


def read_hero_hole_cards(hole_cards_img, reason: str = "hole_cards") -> str:
    """히어로 홀카드를 "Ah Kd" 형식으로 반환한다. 관전 모드라 안 보이거나 AI 호출이
    실패하면(예외를 던지지 않고) 빈 문자열을 반환한다(task.md "에러로 만들지 말 것")."""
    if hole_cards_img is None:
        return ""
    data, latency, fail_reason = _call_vision_json(hole_cards_img, HOLE_CARDS_PROMPT, reason)
    if data is None:
        _log(reason, False, latency, fail_reason)
        return ""
    cards = _format_cards(data.get("cards"), max_count=2)
    _log(reason, True, latency, f"cards={cards!r}")
    return cards


def read_board_cards(
    board_img, expected_count: int, reason: str = "board", known_cards: list[str] | None = None
) -> str:
    """보드 카드를 "As 7h 2c" 형식으로 반환한다. 실패/미확신이면 빈 문자열.

    known_cards를 주면(이전 스트리트에서 이미 확정된 카드) AI에게 새로 추가된
    카드만 읽게 하고, 결과에 known_cards를 그대로 앞에 붙여 돌려준다(task.md
    "보드 카드 재확인 흔들림 수정" - 이미 맞은 카드가 스트리트마다 다시 흔들리는
    문제를 애초에 다시 안 물어봐서 없앤다)."""
    if board_img is None or expected_count <= 0:
        return ""
    known_cards = known_cards or []
    new_needed = expected_count - len(known_cards)
    if new_needed <= 0:
        return " ".join(known_cards)
    data, latency, fail_reason = _call_vision_json(board_img, _board_prompt(expected_count, known_cards), reason)
    if data is None:
        _log(reason, False, latency, fail_reason)
        return ""
    new_cards = _format_cards(data.get("cards"), max_count=new_needed)
    cards = " ".join(known_cards + new_cards.split()) if known_cards else new_cards
    _log(reason, True, latency, f"cards={cards!r}")
    return cards
