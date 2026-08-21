"""보드 카드 장수 감지 (task2.md 지침: 카드 값(Ks, 6c 등)은 OCR로 읽지 않는다).

카드 랭크 폰트가 장식체라 Windows OCR로 전혀 읽히지 않는 것을 확인했다
(main.py 개발 중 실측 테스트, 모든 배율에서 인식 실패). 대신 "카드가 있는
부분은 밝은 흰색, 빈 테이블 펠트는 어둡다"는 성질만 이용해서, 보드 영역 안에서
밝은 부분의 가로 폭을 재고 카드 1장의 대략적인 폭으로 나눠 몇 장이 나왔는지
추정한다. 카드 내부 그림(문양) 때문에 밝기가 잠깐씩 떨어지는 것에 영향받지
않도록, 개별 카드 사이 경계를 찾지 않고 전체 밝은 영역의 시작~끝 폭만 잰다.

task.md "보드 장수 감지 수정"(2026-08-21): 카드 1장의 폭(CARD_UNIT_WIDTH)은
테이블/방마다 렌더링 크기가 달라서 고정 상수 하나로는 한계가 있다(실측 확인:
어떤 6인 테이블은 실제 카드 폭이 66px 보정값보다 작아서, 리버 5장이 계속
4장으로만 잡혔다 - 295px÷66≈4.47이 반올림으로 4가 됨). 그래서 `detect_card_count`
에 `card_unit_width`를 선택적으로 받게 하고, 호출부(dealer_chat_bridge.py)가
AI가 실제로 읽어낸 카드 개수(신뢰할 수 있는 정답)로 그 세션의 실제 카드 폭을
역산해서 넘겨주게 한다 - `measure_bright_width`가 그 역산에 쓸 폭 측정 값을
공개한다. 넘겨받은 값이 없으면(세션 시작 직후 등) 기존 기본값을 그대로 쓴다.
"""
from PIL import Image

# ClubWPT GOLD 실측 기준 (2026-08-18, 사용자 제공 캡처). 보드 중앙에 "ClubWPT GOLD"
# 로고 워터마크가 카드가 없을 때도 항상 떠 있어서, 기존 임계값(150)으로는 로고를
# 카드로 오인했다(실측: 로고=밝은 폭 108px, 카드 3장=132px, 겹치는 범위). 임계값을
# 190까지 올리면 로고(0px)와 실제 카드(132px)가 깔끔히 갈린다.
BRIGHT_THRESHOLD = 190  # 카드(밝음) vs 테이블 펠트/로고(어두움) 판정 기준
# 카드 1장의 대략적인 가로 폭(px) 기본값 - 호출부가 실측 보정값(card_unit_width)을
# 안 넘겨줄 때만 쓰는 폴백이다(영상마다 카드 렌더링 크기가 달라서 이 상수 하나로는
# 모든 테이블에 안 맞는다 - 위 모듈 설명 참고).
CARD_UNIT_WIDTH = 66.0
VALID_COUNTS = (0, 3, 4, 5)

STREET_BY_COUNT = {0: "프리플랍", 3: "플랍", 4: "턴", 5: "리버"}


def _brightness(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _column_brightness(px, x, y0, y1) -> float:
    total = 0.0
    n = 0
    for y in range(y0, y1):
        total += _brightness(px[x, y])
        n += 1
    return total / n if n else 0.0


def measure_bright_width(img: Image.Image, board_region: tuple[int, int, int, int]) -> int:
    """board_region 안에서 밝은 영역(카드가 있는 부분)의 가로 폭(px)을 잰다.
    카드가 하나도 없으면 0. detect_card_count와 dealer_chat_bridge.py의 카드 폭
    자동 보정(AI가 읽은 실제 장수로 역산) 둘 다 이 측정값을 재사용한다."""
    x1, y1, x2, y2 = board_region
    crop = img.crop((x1, y1, x2, y2)).convert("RGB")
    w, h = crop.size
    if w <= 0 or h <= 0:
        return 0
    px = crop.load()

    band_top, band_bot = int(h * 0.3), int(h * 0.7)

    left = None
    for x in range(w):
        if _column_brightness(px, x, band_top, band_bot) >= BRIGHT_THRESHOLD:
            left = x
            break
    if left is None:
        return 0

    right = None
    for x in range(w - 1, -1, -1):
        if _column_brightness(px, x, band_top, band_bot) >= BRIGHT_THRESHOLD:
            right = x
            break

    return (right - left + 1) if right is not None else 0


def detect_card_count(
    img: Image.Image,
    board_region: tuple[int, int, int, int],
    card_unit_width: float | None = None,
) -> int:
    """board_region 안에 카드가 몇 장 나와 있는지 추정한다. 결과는 항상 0/3/4/5 중 하나.

    card_unit_width를 주면(호출부가 그 테이블에서 실측 보정한 값) 그걸로 나누고,
    안 주면(기존 recorder.py 등 호환용) 기본 CARD_UNIT_WIDTH를 그대로 쓴다."""
    width = measure_bright_width(img, board_region)
    if width == 0:
        return 0
    unit = card_unit_width if card_unit_width and card_unit_width > 0 else CARD_UNIT_WIDTH
    estimated = round(width / unit)
    return min(VALID_COUNTS, key=lambda c: abs(c - estimated))


def street_for_count(count: int) -> str:
    return STREET_BY_COUNT.get(count, "프리플랍")
