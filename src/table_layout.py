"""상대 좌표(비율) 테이블 레이아웃.

더 이상 전체 화면 OCR을 하지 않고, 내 홀카드 / 보드카드 / 좌석별 행동 텍스트가
항상 나타나는 고정 위치 영역만 잘라서 OCR한다. 좌표는 코드에 박아두지 않고 JSON
설정 파일(table_layout.json)로 분리해서, 실제 화면에 맞춰 숫자만 조정할 수 있게
한다.

좌표는 특정 캡처 크기 기준 px가 아니라, 게임 창 너비/높이 대비 비율(0.0~1.0)로
저장한다 - 창을 이동하거나 크기를 바꿔도(즉 매 프레임 실제 캡처 크기가 달라져도)
그 순간의 창 크기에 비율만 곱하면 항상 맞는 좌표가 나온다.
"""
import json
import os
from dataclasses import dataclass, field

LAYOUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "table_layout.json")

# 실측 좌표 (2026-08-19 재실측, 코인포커 영상 'KakaoTalk_20260819_124403764.mp4'의
# 35초 지점 프레임, 1030x720 캡처에서 잰 뒤 창 너비/높이 비율로 환산). table_layout.json
# 이 없을 때 이 값으로 새로 만든다. ClubWPT GOLD 좌표는 table_layout.clubwpt.json 에 백업해뒀다.
DEFAULT_LAYOUT = {
    "table_type": "7max",
    "hero_seat_index": 6,
    "board_region": [0.2913, 0.3611, 0.6311, 0.5069],
    "hole_cards_region": [0.4417, 0.7014, 0.5485, 0.8403],
    "hero_action_buttons_region": [0.5728, 0.8403, 0.9369, 0.9722],
    "result_panel_region": None,  # 아직 캡처가 없어 미정
    # 코인포커는 행동 태그가 좌석 박스 좌상단에 겹쳐서 뜨고(한게임/ClubWPT와 다른 위치),
    # "겹"처럼 짧은 글자는 주변 텍스트가 있어야 OCR이 더 잘 잡는 경향이 실측으로 확인돼서,
    # 박스 하단 일부만 자르지 않고 전체를 다 본다(1.0 = 전체 높이).
    "action_zone_bottom_ratio": 1.0,
    # 딜러 버튼이 빨간 원(코인포커). ClubWPT GOLD는 흰 원이었다 - 게임마다 달라서
    # 여기서 설정하고, dealer_position.py는 이 목록에 있는 색만 확인한다.
    "dealer_marker_colors": ["red"],
    # 카드 뒷면(빨간 다이아몬드 패턴)이 좌석 박스 위쪽과 겹쳐서 딜러 버튼과 색이
    # 비슷해 오탐이 났다(실측 확인) - 딜러 탐색 시에만 위쪽을 잘라낸다(창 높이 비율).
    "dealer_zone_top_trim": 0.0167,
    # 이긴 좌석의 홀카드 위에 뜨는 "+금액"을 잡기 위해 좌석 박스 위쪽으로 늘리는
    # 비율(창 높이 기준). 0이면 result_panel_region 방식만 쓴다.
    "result_zone_top_extend": 0.1389,
    "seats": [
        {"index": 0, "label": "상단좌", "is_hero": False, "region": [0.1456, 0.2431, 0.2961, 0.3542]},
        {"index": 1, "label": "상단우", "is_hero": False, "region": [0.6796, 0.2431, 0.8301, 0.3542]},
        {"index": 2, "label": "중단좌", "is_hero": False, "region": [0.0437, 0.4861, 0.2136, 0.5833]},
        {"index": 3, "label": "중단우", "is_hero": False, "region": [0.7718, 0.4792, 0.9417, 0.5833]},
        {"index": 4, "label": "하단좌", "is_hero": False, "region": [0.1456, 0.7014, 0.2961, 0.8125]},
        {"index": 5, "label": "하단우", "is_hero": False, "region": [0.6699, 0.7014, 0.8252, 0.8125]},
        {"index": 6, "label": "하단중앙 (나)", "is_hero": True, "region": [0.4078, 0.6944, 0.5922, 0.9722]},
    ],
    "clockwise_order": [0, 1, 3, 5, 6, 4, 2],
}


@dataclass
class SeatRegion:
    index: int
    label: str
    region: tuple[float, float, float, float]
    is_hero: bool = False


@dataclass
class TableLayout:
    table_type: str
    hole_cards_region: tuple[float, float, float, float]
    board_region: tuple[float, float, float, float]
    result_panel_region: tuple[float, float, float, float] | None = None
    hero_action_buttons_region: tuple[float, float, float, float] | None = None
    seats: list[SeatRegion] = field(default_factory=list)
    clockwise_order: list[int] = field(default_factory=list)
    hero_seat_index: int | None = None
    # 좌석 박스에서 행동 텍스트 크롭에 쓸 "아래쪽부터 몇 %"(1.0=전체). 기존 ClubWPT
    # 기본값(0.35, 박스 하단부만)과 호환되도록 기본값은 0.35로 둔다.
    action_zone_bottom_ratio: float = 0.35
    # 딜러 버튼 원 내부 색으로 인정할 목록. 기존 로직(흰색)과 호환되도록 기본값은 white.
    dealer_marker_colors: list[str] = field(default_factory=lambda: ["white"])
    # 딜러 버튼 탐색 영역을 만들 때 좌석 박스 "위쪽"을 창 높이 대비 몇 %만큼 잘라내고
    # 시작할지. 코인포커는 카드 뒷면(빨간 다이아몬드 패턴)이 좌석 박스 위쪽과 겹쳐
    # 있어서, 딜러 버튼과 색이 비슷해 오탐이 났다(실측 확인) - 그 부분만 잘라낸다.
    # 행동 텍스트 인식 영역(action_zone)에는 영향 없다(그건 원본 박스를 그대로 쓴다).
    dealer_zone_top_trim: float = 0.0
    # 코인포커는 ClubWPT의 "결과 패널"(단일 영역) 같은 게 없고, 대신 이긴 좌석의
    # 홀카드 위에 "+금액"이 잠깐 뜬다(실측 확인). 그래서 결과 패널 하나 대신, 좌석
    # 박스 "위쪽"을 창 높이 대비 이 비율만큼 늘려서 그 좌석마다 승리 금액이 뜨는지
    # 본다. 0이면 기존 result_panel_region 방식만 쓴다(ClubWPT 등 호환, 동작 안 바뀜).
    result_zone_top_extend: float = 0.0

    def seat_by_index(self, index: int) -> SeatRegion | None:
        return next((s for s in self.seats if s.index == index), None)

    def hero_seat(self) -> SeatRegion | None:
        if self.hero_seat_index is None:
            return None
        return self.seat_by_index(self.hero_seat_index)


def _to_layout(data: dict) -> TableLayout:
    seats = [
        SeatRegion(
            index=s["index"],
            label=s.get("label", ""),
            region=tuple(s["region"]),
            is_hero=bool(s.get("is_hero", False)),
        )
        for s in data.get("seats", [])
    ]
    clockwise_order = data.get("clockwise_order") or [s.index for s in seats]
    hero_buttons = data.get("hero_action_buttons_region")
    return TableLayout(
        table_type=data.get("table_type", "5max"),
        hole_cards_region=tuple(data["hole_cards_region"]),
        board_region=tuple(data["board_region"]),
        result_panel_region=tuple(data["result_panel_region"]) if data.get("result_panel_region") else None,
        hero_action_buttons_region=tuple(hero_buttons) if hero_buttons else None,
        seats=seats,
        clockwise_order=list(clockwise_order),
        hero_seat_index=data.get("hero_seat_index"),
        action_zone_bottom_ratio=float(data.get("action_zone_bottom_ratio", 0.35)),
        dealer_marker_colors=list(data.get("dealer_marker_colors") or ["white"]),
        dealer_zone_top_trim=float(data.get("dealer_zone_top_trim", 0.0)),
        result_zone_top_extend=float(data.get("result_zone_top_extend", 0.0)),
    )


def _save_dict(data: dict) -> None:
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_layout() -> TableLayout:
    """table_layout.json 을 읽는다. 없으면 task.md 샘플 좌표로 새로 만든다."""
    if os.path.exists(LAYOUT_PATH):
        try:
            with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _to_layout(data)
        except Exception:
            pass
    _save_dict(DEFAULT_LAYOUT)
    return _to_layout(DEFAULT_LAYOUT)


def resolve_ratio_region(region: tuple[float, float, float, float], capture_size) -> tuple[int, int, int, int]:
    """0.0~1.0 비율 좌표를, 그 순간 실제로 캡처된 창 크기(capture_size)에 맞는
    실제 픽셀 좌표로 환산한다. 창이 이동/리사이즈돼서 매 프레임 캡처 크기가
    달라져도, 그때그때의 크기만 넣으면 항상 맞는 좌표가 나온다."""
    cap_w, cap_h = capture_size
    x1, y1, x2, y2 = region
    return (round(x1 * cap_w), round(y1 * cap_h), round(x2 * cap_w), round(y2 * cap_h))
