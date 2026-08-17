"""화면에 보이는 '플레이어 닉네임'의 위치를 계속 추적해서, 위치가 고정되지
않은 행동 텍스트("Fold" 등)가 어느 플레이어의 행동인지 가장 가까운 닉네임으로
추정한다.

홀덤 게임 화면은 보통 좌석마다 닉네임이 항상 떠 있고, 그 근처에 잠깐씩
행동 텍스트(Fold/Call/Raise 등)가 나타났다가 사라진다. 닉네임과 행동 텍스트가
같은 한 줄로 인식되지 않는 경우가 많으므로, 화면 위치로 서로 연결한다.
"""
import math
import re
from dataclasses import dataclass

from .action_parser import matches_any_keyword

# 닉네임으로 보기엔 너무 길거나(문장), 숫자만 있거나 한 텍스트는 걸러낸다.
_MIN_LEN = 2
_MAX_LEN = 14
_HAS_LETTER = re.compile(r"[0-9A-Za-z가-힣]")
_ONLY_DIGITS_PUNCT = re.compile(r"^[0-9,\.\s원칩\-()%]+$")
# "10만골드", "5,000원", "100칩"처럼 숫자+단위로 된 금액/재화 표시는 닉네임이 아니다.
_AMOUNT_LIKE = re.compile(r"\d\s*(만|억|천|원|골드|칩|포인트|point|pt|개)", re.I)


def looks_like_player_name(text: str) -> bool:
    t = text.strip()
    if not (_MIN_LEN <= len(t) <= _MAX_LEN):
        return False
    if not _HAS_LETTER.search(t):
        return False
    if _ONLY_DIGITS_PUNCT.match(t):
        return False
    if _AMOUNT_LIKE.search(t):
        return False
    if len(t.split()) > 2:
        return False  # 문장처럼 단어가 여러 개면 닉네임이 아닐 가능성이 높다.
    if matches_any_keyword(t):
        return False  # 행동/스트리트/새 게임 등 키워드 자체는 닉네임이 아니다.
    return True


@dataclass
class _Entry:
    name: str
    x: float
    y: float
    last_seen: int
    seen_count: int = 1


class PlayerRegistry:
    def __init__(self, bucket: int = 45, max_age_frames: int = 300):
        self.bucket = bucket
        self.max_age_frames = max_age_frames
        self._entries: dict[tuple, _Entry] = {}
        self._frame = 0

    def _bucket_key(self, x: float, y: float) -> tuple:
        return (round(x / self.bucket), round(y / self.bucket))

    def update(self, lines: list) -> None:
        """lines: OcrLine(text, x, y) 목록. 매 프레임 전체 인식 결과로 호출한다."""
        self._frame += 1
        for line in lines:
            text = line.text.strip()
            if not looks_like_player_name(text):
                continue
            key = self._bucket_key(line.x, line.y)
            existing = self._entries.get(key)
            if existing is not None:
                existing.name = text
                existing.x, existing.y = line.x, line.y
                existing.last_seen = self._frame
                existing.seen_count += 1
            else:
                self._entries[key] = _Entry(name=text, x=line.x, y=line.y, last_seen=self._frame)

        if self._frame % 50 == 0:
            self._prune()

    def entries(self) -> list[_Entry]:
        return list(self._entries.values())

    def _prune(self) -> None:
        stale = [k for k, e in self._entries.items() if self._frame - e.last_seen > self.max_age_frames]
        for k in stale:
            del self._entries[k]

    def find_nearest(self, x: float, y: float, max_dist: float = 160.0) -> str:
        best_name = ""
        best_dist = max_dist
        for entry in self._entries.values():
            dist = math.hypot(entry.x - x, entry.y - y)
            if dist < best_dist:
                best_dist = dist
                best_name = entry.name
        return best_name
