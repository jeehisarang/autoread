"""좌석(화면 위치) <-> 포지션(UTG/HJ/CO/BTN/SB/BB) 매핑.

딜러 버튼을 화면에서 직접(그림/아이콘) 인식하는 기능은 이번 범위에 없으므로,
"6명이 계속 앉아 있고, 버튼은 매 핸드 한 자리씩 이동한다"는 표준 포커 규칙을
그대로 이용해 게임 번호만으로 포지션을 계산한다. 즉, 실제 버튼 위치를 화면에서
읽는 것이 아니라 계산으로 추정하는 방식이며, 중간에 플레이어가 자리를 옮기거나
테이블 인원이 바뀌면 실제와 달라질 수 있다.

동작 순서
1. player_tracker.PlayerRegistry 로 화면에 안정적으로 계속 보이는 닉네임(=좌석)들의
   위치를 모은다.
2. 그중 화면 하단 중앙에 가장 가까운 좌석을 '내 자리'로 정하고, 나머지 좌석들을
   내 자리를 기준으로 화면상 시계 방향 각도 순으로 정렬해서 테이블 순서를 고정한다.
   (한 번 확정되면 세션 동안 유지한다.)
3. 1게임에서는 내 자리를 UTG로 두고, 매 핸드(게임)마다 모든 좌석의 포지션이 한 칸씩
   당겨지도록(UTG→HJ→CO→BTN→SB→BB→UTG...) 계산한다. 이는 실제로 버튼이 한 자리씩
   도는 것과 동일한 효과를 낸다.
"""
import math

from .player_tracker import PlayerRegistry

POSITIONS_6MAX = ["UTG", "HJ", "CO", "BTN", "SB", "BB"]

# 화면 좌표는 아래로 갈수록 y가 커지므로, 각도가 증가하는 방향이 화면상 시계 방향이 된다.
# 실제 게임에서 반대로 나온다면 이 값을 False로 바꾸면 방향이 뒤집힌다.
SEAT_ORDER_CLOCKWISE = True

MIN_SEEN_COUNT = 3  # 이 정도는 안정적으로 보여야 '좌석'으로 확정한다.
MAX_SEATS = 6


class SeatMapper:
    def __init__(self):
        self._seats: list[tuple[float, float]] = []  # index 0 = 내 자리, 이후 시계 방향
        self._locked = False

    def is_locked(self) -> bool:
        return self._locked

    def try_lock(self, registry: PlayerRegistry, capture_w: float, capture_h: float) -> bool:
        """충분히 안정적인 좌석 후보가 모이면 좌석 배치를 확정한다."""
        if self._locked:
            return True
        candidates = [e for e in registry.entries() if e.seen_count >= MIN_SEEN_COUNT]
        if len(candidates) < 2:
            return False  # 최소 나 + 상대 1명은 있어야 의미가 있다.

        candidates.sort(key=lambda e: -e.seen_count)
        candidates = candidates[:MAX_SEATS]

        hero_ref_x, hero_ref_y = capture_w * 0.5, capture_h * 0.85
        hero_entry = min(candidates, key=lambda e: math.hypot(e.x - hero_ref_x, e.y - hero_ref_y))
        others = [e for e in candidates if e is not hero_entry]

        cx = sum(e.x for e in candidates) / len(candidates)
        cy = sum(e.y for e in candidates) / len(candidates)
        hero_angle = math.atan2(hero_entry.y - cy, hero_entry.x - cx)

        def rel_angle(e):
            a = math.atan2(e.y - cy, e.x - cx) - hero_angle
            a %= 2 * math.pi
            return a if SEAT_ORDER_CLOCKWISE else (2 * math.pi - a) % (2 * math.pi)

        others.sort(key=rel_angle)

        self._seats = [(hero_entry.x, hero_entry.y)] + [(e.x, e.y) for e in others]
        self._locked = True
        return True

    def _labels(self) -> list[str]:
        n = len(self._seats)
        if n >= 6:
            return POSITIONS_6MAX
        return POSITIONS_6MAX[-n:]  # 6명 미만이면 BB 쪽(숏핸디드 관례)부터 사용

    def position_for(self, x: float, y: float, hand_no: int, max_dist: float = 220.0):
        """(x, y) 근처의 좌석을 찾아 (포지션 라벨, 내 자리 여부)를 반환한다. 못 찾으면 (None, False)."""
        if not self._locked:
            return None, False

        best_i, best_d = None, max_dist
        for i, (sx, sy) in enumerate(self._seats):
            d = math.hypot(sx - x, sy - y)
            if d < best_d:
                best_d, best_i = d, i
        if best_i is None:
            return None, False

        labels = self._labels()
        n = len(labels)
        label_index = (best_i - (hand_no - 1)) % n
        return labels[label_index], best_i == 0
