"""딜러 채팅 파서 (task.md "딜러 채팅 파서 1차 구현").

기존 메인 파이프라인(recorder.py의 아우라/밝기/딜러버튼/동기화 로직)은 건드리지
않는다 - 딜러 채팅 전용 별도 창을 캡처해서 독립적으로 동작하는 모듈이다.

딜러 채팅 창은 프리플랍/플랍/턴/리버 4개 컬럼으로 나뉘어 있고, 각 컬럼 안에서
행동이 쌓일수록 아래로 늘어난다(행이 고정 좌표가 아니다). 그래서:
  1. 컬럼 전체를 한 번에 OCR한다.
  2. 나온 텍스트 줄들을 y좌표 순으로 훑으면서, "행동 단어가 있는 줄"을 기준(anchor)
     삼아 바로 위 1~2줄(포지션 코드, 이름)을 같은 항목으로 묶는다.

실제 창을 캡처해서 확인해보니(2026-08-20, 실제 딜러 채팅 창 800x617 캡처), 각
채팅 항목은 위에서부터: [이름] [포지션 코드(UTG/BTN/SB/BB/CO 등)] [행동+금액]
이렇게 세 줄로 따로 인식된다. 포지션 배지가 원형 글자라 OCR이 안 될 거라고 봤던
최초 가정(이전 task.md)과 달리, 실측 결과 포지션 코드 텍스트 자체가 대부분
정상적으로 OCR된다 - 그래서 색상 판별 대신 OCR 텍스트로 바로 읽는다(더 정확하고
구현도 단순함). 다만 가끔(실측 확인, 예: "SLIDerDave19" 항목) 포지션 줄 자체가
인식 안 되는 경우가 있어, 그런 경우 이름 줄만으로 처리하고 포지션은 "?"로 둔다.
"""
import json
import os
import re
from dataclasses import dataclass, field

from PIL import Image

from . import ocr_engine
from .action_parser import ACTION_PATTERNS, extract_amount_from_text
from .table_layout import resolve_ratio_region

LAYOUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dealer_chat_layout.json"
)

DEFAULT_LAYOUT = {
    "note": (
        "2026-08-20 실제 딜러 채팅 창(800x617 캡처)으로 재실측. 4개 컬럼은 창 폭을 "
        "정확히 4등분(196px씩)한 것과 일치했다. y1=0.165는 상단 필터 버튼줄/스트리트 "
        "제목/팟 금액(플랍 이후는 보드카드까지) 영역을 제외한 값 - 실제 캡처에서 "
        "헤더 마지막 줄(팟 금액) y≈87/578이었다. row_lookback_px는 실측한 이름→행동 "
        "줄 간격(약 24px)보다 여유 있게 잡았다(행 사이 간격 46px+와는 확실히 구분됨)."
    ),
    "streets": ["프리플랍", "플랍", "턴", "리버"],
    # [x1, y1, x2, y2] 비율 좌표(창 크기 대비).
    "columns": [
        [0.0, 0.165, 0.25, 1.0],
        [0.25, 0.165, 0.5, 1.0],
        [0.5, 0.165, 0.75, 1.0],
        [0.75, 0.165, 1.0, 1.0],
    ],
    # 행동 줄을 기준으로 위로 이만큼(px, OCR 크롭 원본 기준) 안에 있는 줄까지만
    # "같은 항목"(이름/포지션)으로 묶는다. 실측: 이름->포지션 14px, 포지션->행동
    # 10px(항목당 총 24px 안팎), 반면 서로 다른 항목 사이는 46px 이상이었다.
    "row_lookback_px": 40,
    # 히어로 닉네임 접두어 매칭 최소 글자 수(너무 짧으면 다른 사람과 오매칭 위험).
    "hero_min_prefix_len": 5,
}


@dataclass
class DealerChatLayout:
    streets: list[str] = field(default_factory=lambda: list(DEFAULT_LAYOUT["streets"]))
    columns: list[tuple[float, float, float, float]] = field(default_factory=list)
    row_lookback_px: float = 40.0
    hero_min_prefix_len: int = 5


def _to_layout(data: dict) -> DealerChatLayout:
    return DealerChatLayout(
        streets=list(data.get("streets") or DEFAULT_LAYOUT["streets"]),
        columns=[tuple(c) for c in data.get("columns") or DEFAULT_LAYOUT["columns"]],
        row_lookback_px=float(data.get("row_lookback_px", 40.0)),
        hero_min_prefix_len=int(data.get("hero_min_prefix_len", 5)),
    )


def _save_default() -> None:
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_LAYOUT, f, ensure_ascii=False, indent=2)


def load_layout() -> DealerChatLayout:
    """dealer_chat_layout.json 을 읽는다. 없으면 기본값으로 새로 만든다."""
    if os.path.exists(LAYOUT_PATH):
        try:
            with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _to_layout(data)
        except Exception:
            pass
    _save_default()
    return _to_layout(DEFAULT_LAYOUT)


# ---------- 행동 판별 ----------
# task.md 2절: Fold / Call / Raise / Check / Bet / Ante. action_parser의 기존
# ACTION_PATTERNS(실측으로 검증된 OCR 오인식 보정 포함, 예: "FLD"/"F0LD")를 그대로
# 재사용하고, 거기 없는 Ante만 추가한다.
ROW_ACTION_PATTERNS: list[tuple[str, re.Pattern]] = list(ACTION_PATTERNS) + [
    ("Ante", re.compile(r"(ante|앤티)", re.I)),
]
ACTIONS_WITHOUT_AMOUNT = {"Fold", "Check"}

# 포지션 코드 화이트리스트. UTG+1/UTG+2/MP+1 등 '+' 뒤 숫자는 OCR이 종종 I/L/O로
# 오인식하는 게 실측 확인돼서("UTG+I") 그 한 글자만 보정한다.
_POSITION_SIMPLE = {"BTN", "SB", "BB", "UTG", "MP", "HJ", "CO", "LJ", "EP"}
_POSITION_PLUS_RE = re.compile(r"^(UTG|MP)\+([0-9ILO])$")
_POSITION_DIGIT_FIX = {"I": "1", "L": "1", "O": "0"}


def _normalize_position(text: str) -> str | None:
    t = re.sub(r"[^A-Za-z0-9+]", "", text or "").upper()
    if not t:
        return None
    if t in _POSITION_SIMPLE:
        return t
    m = _POSITION_PLUS_RE.match(t)
    if m:
        base, digit_raw = m.group(1), m.group(2)
        digit = _POSITION_DIGIT_FIX.get(digit_raw, digit_raw)
        if digit in "123":
            return f"{base}+{digit}"
    return None


@dataclass
class ChatRow:
    street: str
    position: str  # "UTG" 등, 인식 못 하면 "?"
    player: str
    action: str
    amount: str
    is_hero: bool = False
    raw_text: str = ""


def _parse_action_line(text: str) -> tuple[str, str] | None:
    """한 줄 텍스트에서 (행동, 금액)을 추출한다. 행동 단어가 없으면 None."""
    for action_name, pattern in ROW_ACTION_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        amount = "" if action_name in ACTIONS_WITHOUT_AMOUNT else extract_amount_from_text(text[m.end():])
        return action_name, amount
    return None


def _extract_rows(lines: list, lookback_px: float) -> list[tuple[str, str, str, str, str]]:
    """OCR 줄 목록에서 (포지션, 이름, 행동, 금액, 원문) 튜플 목록을 만든다.

    실측 확인된 항목 구조(위에서부터 이름 -> 포지션 코드 -> 행동+금액) 대로, 행동
    단어가 있는 줄을 기준(anchor)으로 삼아 그 줄 바로 위, lookback_px 안에 있는
    줄들을 같은 항목의 이름/포지션으로 묶는다. 포지션 줄이 인식 안 된 경우(실측
    확인된 케이스)에는 이름만 쓰고 포지션은 "?"로 남긴다."""
    ordered = sorted(lines, key=lambda l: l.y)
    rows = []
    for i, line in enumerate(ordered):
        parsed = _parse_action_line(line.text)
        if parsed is None:
            continue
        action, amount = parsed

        candidates = []
        j = i - 1
        while j >= 0 and line.y - ordered[j].y <= lookback_px:
            candidates.append(ordered[j])
            j -= 1
        candidates.reverse()  # 이제 위->아래(이름 -> 포지션) 순서

        position, name = "?", ""
        if len(candidates) >= 2:
            name = candidates[-2].text.strip()
            pos = _normalize_position(candidates[-1].text)
            if pos:
                position = pos
            else:
                # 후보 마지막 줄이 포지션 코드가 아니면(오탐) 그냥 이름으로 취급
                name = candidates[-1].text.strip()
        elif len(candidates) == 1:
            pos = _normalize_position(candidates[0].text)
            if pos:
                position = pos
            else:
                name = candidates[0].text.strip()

        rows.append((position, name, action, amount, line.text))
    return rows


# ---------- 히어로 매칭 ----------
_NON_ALNUM = re.compile(r"[^0-9a-zA-Z가-힣]")


def _normalize_name(name: str) -> str:
    return _NON_ALNUM.sub("", name or "").lower()


def _within_edit_distance_one(a: str, b: str) -> bool:
    """a, b의 편집거리(삽입/삭제/치환)가 1 이하인지. 길이 차가 2 이상이면 무조건
    False(짧은 문자열이라 굳이 DP 안 쓰고 바로 끝낸다)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # 치환 한 글자만 다른지 - 다른 글자가 두 개 이상이면 False
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    # 길이가 1 차이 - 짧은 쪽이 긴 쪽에서 한 글자 빼면 나오는지
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        elif not skipped:
            skipped = True
            j += 1
        else:
            return False
    return True


def _edit_distance_at_most(a: str, b: str, max_dist: int) -> bool:
    """a, b의 편집거리가 max_dist 이하인지 (일반 DP, 닉네임 길이라 작아서 O(n*m)도 충분)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return False
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[lb] <= max_dist


def names_match_prefix(hero_name: str, chat_name: str, min_len: int = 5) -> bool:
    """말줄임표로 짧게 잘린 닉네임끼리도 접두어로 매칭한다(task.md [D]).
    예: "M0mSp4gh…" <-> "M0mSp4ghetti1"(실측: OCR은 말줄임표를 "•••"로 읽는다 -
    어차피 영문/숫자/한글만 남기고 다 지우므로 상관없다). 둘 중 어느 쪽이 잘렸는지
    모르므로 양방향으로 본다. 너무 짧은 이름끼리는(min_len 미만) 접두어만으로
    오매칭 위험이 커서 완전 일치를 요구한다.

    task.md "히어로 매칭 + 폴드 후 기록 중단"(2026-08-20): 실측 확인된 문제 -
    짧은 이름(예: "Vincy", 5자)은 말줄임표로 잘릴 일이 없는데도, OCR이 글자
    하나를 다르게 읽어서("Vincy" -> "Vincv") 접두어 매칭으로 전혀 못 잡는 경우가
    있었다(길이는 같은데 중간 글자 하나만 다름 - 접두어 관계 자체가 성립 안 함).
    그래서 길이가 min_len 이상이고 서로 거의 같은 길이(1자 이내 차이)인 경우엔
    편집거리 1 이하도 같은 사람으로 본다. 너무 짧은 이름끼리 이걸 허용하면
    오매칭 위험이 커서(예: 3~4자 이름은 한 글자만 달라도 완전히 다른 사람일
    수 있음) min_len 이상일 때만 적용한다."""
    a, b = _normalize_name(hero_name), _normalize_name(chat_name)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < min_len:
        return shorter == longer
    if longer.startswith(shorter):
        return True
    if abs(len(a) - len(b)) <= 1 and _within_edit_distance_one(a, b):
        return True
    # 실측 확인된 문제(2026-08-21): OCR이 스타일체 폰트에서 "ly" 같은 두 글자를
    # 로마 숫자 기호(예: "ⅳ")처럼 낯선 유니코드 한 글자로 뭉개 읽고, 그 글자는
    # _NON_ALNUM 필터에 걸려 통째로 사라진다 - 원래 이름보다 2글자 짧아지는
    # 식으로 어긋난다. 이런 경우까지 잡으려고, 충분히 긴 이름(min_len*2 이상,
    # 오매칭 위험이 낮음)에 한해 앞부분 편집거리 2까지 허용한다.
    if len(shorter) >= min_len + 3:
        window = longer[: len(shorter) + 2]
        if _edit_distance_at_most(shorter, window, 2):
            return True
    return False


# ---------- 컬럼/프레임 파싱 ----------
def parse_column(
    column_img: Image.Image,
    street: str,
    layout: DealerChatLayout,
    hero_name: str = "",
    ocr_lang: str = "ko",
) -> list[ChatRow]:
    lines = ocr_engine.recognize_lines(column_img, lang=ocr_lang)
    rows = []
    for position, player, action, amount, raw_text in _extract_rows(lines, layout.row_lookback_px):
        is_hero = bool(hero_name) and names_match_prefix(hero_name, player, layout.hero_min_prefix_len)
        rows.append(ChatRow(street=street, position=position, player=player, action=action,
                             amount=amount, is_hero=is_hero, raw_text=raw_text))
    return rows


def parse_dealer_chat(
    chat_img: Image.Image,
    layout: DealerChatLayout | None = None,
    hero_name: str = "",
    ocr_lang: str = "ko",
) -> dict[str, list[ChatRow]]:
    """딜러 채팅 창 캡처 이미지 전체를 받아, 스트리트별 ChatRow 목록을 반환한다."""
    layout = layout or load_layout()
    result: dict[str, list[ChatRow]] = {}
    for street, col_region in zip(layout.streets, layout.columns):
        region_px = resolve_ratio_region(col_region, chat_img.size)
        try:
            crop = chat_img.crop(region_px)
        except Exception:
            result[street] = []
            continue
        result[street] = parse_column(crop, street, layout, hero_name=hero_name, ocr_lang=ocr_lang)
    return result


# ---------- 출력 포맷 ----------
HERO_LABEL = "나"


def _format_row(row: ChatRow) -> str:
    # recorder.py의 기존 출력 포맷(_format_action_line/_format_hero_line)과
    # 맞춘다 - task.md [E] "기존 최종 포맷에 최대한 가깝게".
    if row.is_hero:
        text = f"{HERO_LABEL:<5}: {row.action}"
    else:
        text = f"{row.position:<5} ] {row.action}"
    if row.amount:
        text += f" {row.amount}"
    return text


def format_output(parsed: dict[str, list[ChatRow]], layout: DealerChatLayout | None = None) -> str:
    """task.md "로그 안정화 4종" 1절: Ante는 이 스냅샷 출력에서도 제외한다(실시간
    경로인 dealer_chat_recorder.py와 동작을 맞춘다)."""
    layout = layout or load_layout()
    lines = []
    for street in layout.streets:
        rows = [r for r in (parsed.get(street) or []) if r.action != "Ante"]
        if not rows:
            continue
        lines.append(street)
        for row in rows:
            lines.append(_format_row(row))
        lines.append("")
    return "\n".join(lines).rstrip("\n")
