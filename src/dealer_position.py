"""딜러 버튼 인식 + 딜러 기준 상대 포지션 계산 (task.md 3절).

각 좌석 고정 영역의 OCR 결과에서 딜러 버튼 표시를 찾아 현재 딜러 좌석을 판별한다.
게임마다 표시 형태가 달라서 두 가지를 모두 허용한다:
- 한게임 홀덤: "DEALER" 전체 단어 (OCR이 첫 글자를 'O'로 잘못 읽는 경우가 실측
  확인되어 "OEALER"로도 나옴 -> 뒤쪽 'EALER'만 확인해서 오탐을 줄인다)
- ClubWPT GOLD: 원 안에 글자 "D" 하나만 (실측 확인). 한 줄 전체가 정확히 "D"일
  때만 인정해서, 다른 단어 속 D 글자를 오인하지 않게 한다.

포지션은 딜러 좌석을 기준으로, table_layout.json의 clockwise_order(테이블을
실제로 도는 시계방향 좌석 순서)를 따라 '딜러', '딜러+1', '딜러+2' ... 로 계산한다.

ClubWPT GOLD의 "D" 표시는 실측으로 OCR이 원리적으로 못 읽는 것을 확인했다 - 합성
테스트 결과, 완전히 고립된 글자 하나("D")는 주변에 다른 텍스트가 붙어있지 않으면
Windows OCR이 아예 텍스트로 인식하지 않는다(빈 결과). 그래서 이 표시는 OCR 대신
opencv의 원 검출(Hough Circle)로 잡는다. 다만 아바타/코인 아이콘도 원형이라 그냥
원만 찾으면 오탐이 나서(실측 확인), 찾은 원 후보의 "내부 색이 실제로 딜러 버튼
색인지"까지 같이 확인한다.

게임마다 버튼 색이 다르다(ClubWPT GOLD는 흰 원, 코인포커는 빨간 원 - 둘 다 실측
확인). table_layout.json의 dealer_marker_colors 목록에 있는 색만 인정한다:
- "white": 실측 결과 진짜 버튼은 원 내부 평균 밝기가 188~194인 반면 아바타/코인
  등 다른 원형 물체는 70~93으로 뚜렷하게 갈렸다. (R,G,B 모두 밝으면 흰색/회색이므로
  그레이스케일 밝기 기준과 결과가 같다 - 기존 로직 그대로 유지.)
- "red": 실측 결과 원 내부 평균 RGB가 (152, 50, 63) 정도로 R이 G/B보다 뚜렷하게
  높았다.
"""
import re

import cv2
import numpy as np

DEALER_PATTERN = re.compile(r"EALER|^D$", re.I)

DEALER_CIRCLE_MIN_RADIUS = 10
DEALER_CIRCLE_MAX_RADIUS = 20
DEALER_CIRCLE_MIN_INTERIOR_BRIGHTNESS = 150  # white 판정: 실측 188~194 vs 70~93

# red 판정: 실측 원 내부 평균 RGB (152, 50, 63), 그리고 조명/음영이 진 다른 프레임
# 에서는 (73.8, 13.1, 29.2)처럼 전체적으로 더 어둡게 나오는 경우도 확인했다. 절대
# 밝기(R)는 편차가 커서 느슨하게, R이 G/B보다 뚜렷이 높다는 상대적 차이는 엄격하게
# 잡는다 - 이게 밝기 변화에 더 안정적이었다(실측 비교).
DEALER_CIRCLE_RED_MIN_R = 60
DEALER_CIRCLE_RED_MIN_R_MINUS_G = 30
DEALER_CIRCLE_RED_MIN_R_MINUS_B = 20


def find_dealer_seat(seat_lines: dict) -> int | None:
    """seat_lines: {seat_index: [OcrLine, ...]}. 'DEALER' 전체 단어(한게임 스타일)가
    보이는 좌석 index를 반환. ClubWPT GOLD처럼 고립된 'D' 한 글자만 있는 경우는
    OCR로 원리적으로 못 읽으므로(위 설명 참고) find_dealer_seat_by_pixel을 쓴다.
    여러 좌석에서 동시에 검출되면(오탐 가능성) None을 반환해 이전 값을 유지하게 한다."""
    found = [idx for idx, lines in seat_lines.items() if any(DEALER_PATTERN.search(l.text) for l in lines)]
    if len(found) == 1:
        return found[0]
    return None


def _mean_rgb_in_circle(rgb: "np.ndarray", cx: float, cy: float, r: float):
    """원 내부 픽셀들의 평균 (R,G,B)를 반환한다. 못 구하면 None."""
    h, w = rgb.shape[:2]
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r))
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r))
    if x1 <= x0 or y1 <= y0:
        return None
    patch = rgb[y0:y1, x0:x1].astype(float)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    if not mask.any():
        return None
    return patch[mask].mean(axis=0)  # [R, G, B]


def _circle_color_matches(mean_rgb, colors) -> bool:
    """원 내부 평균 RGB가 colors 목록(예: ["white"], ["red"]) 중 하나에 해당하는지."""
    if mean_rgb is None:
        return False
    r, g, b = mean_rgb
    for color in colors:
        if color == "white":
            # R,G,B 전부 밝으면 흰색/회색 - 기존 그레이스케일 밝기 판정과 결과가 같다.
            if r >= DEALER_CIRCLE_MIN_INTERIOR_BRIGHTNESS and g >= DEALER_CIRCLE_MIN_INTERIOR_BRIGHTNESS \
                    and b >= DEALER_CIRCLE_MIN_INTERIOR_BRIGHTNESS:
                return True
        elif color == "red":
            if r >= DEALER_CIRCLE_RED_MIN_R and (r - g) >= DEALER_CIRCLE_RED_MIN_R_MINUS_G \
                    and (r - b) >= DEALER_CIRCLE_RED_MIN_R_MINUS_B:
                return True
    return False


def _has_dealer_button_circle(crop, colors: list[str] = ("white",)) -> bool:
    """crop 안에 딜러 버튼(원 + 글자)이 있는지 픽셀로 판별한다. colors에 담긴 색
    중 하나라도 맞으면 인정한다(게임마다 버튼 색이 다르다 - 위 모듈 설명 참고)."""
    if crop is None:
        return False
    rgb_img = crop.convert("RGB")
    gray = np.array(rgb_img.convert("L"))
    if gray.shape[0] < 20 or gray.shape[1] < 20:
        return False
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=30, param1=80, param2=22,
        minRadius=DEALER_CIRCLE_MIN_RADIUS, maxRadius=DEALER_CIRCLE_MAX_RADIUS,
    )
    if circles is None:
        return False
    rgb = np.array(rgb_img)
    for cx, cy, r in circles[0]:
        mean_rgb = _mean_rgb_in_circle(rgb, cx, cy, r * 0.8)
        if _circle_color_matches(mean_rgb, colors):
            return True
    return False


def find_dealer_seat_by_pixel(seat_crops: dict, colors: list[str] = ("white",)) -> int | None:
    """seat_crops: {seat_index: PIL.Image}. 딜러 버튼(원)이 보이는 좌석 index를 반환.
    colors: 인정할 원 내부 색 목록(예: ["white"] ClubWPT GOLD, ["red"] 코인포커).
    여러 좌석에서 동시에 검출되면(오탐 가능성) None을 반환해 이전 값을 유지하게 한다."""
    found = [idx for idx, crop in seat_crops.items() if _has_dealer_button_circle(crop, colors)]
    if len(found) == 1:
        return found[0]
    return None


def position_label(seat_index: int, dealer_seat_index: int, clockwise_order: list[int]) -> str:
    """딜러 좌석 기준 seat_index의 상대 포지션 라벨을 계산한다 (딜러/딜러+1/...)."""
    if seat_index not in clockwise_order or dealer_seat_index not in clockwise_order:
        return "?"
    n = len(clockwise_order)
    dealer_pos = clockwise_order.index(dealer_seat_index)
    seat_pos = clockwise_order.index(seat_index)
    offset = (seat_pos - dealer_pos) % n
    return "딜러" if offset == 0 else f"딜러+{offset}"
