"""OCR로 매 프레임 인식된 텍스트 줄들 중, '새로 나타난 줄'만 골라낸다.

같은 화면을 반복 캡처하면 매번 같은 줄들이 다시 인식되므로,
최근에 이미 기록한 줄은 걸러내고 새로 등장한 줄만 반환한다.

텍스트뿐 아니라 화면 위치(좌표)도 함께 키로 사용한다. 홀덤 게임에서는
플레이어 좌석마다 같은 단어("Fold" 등)가 서로 다른 위치에서 나타날 수 있으므로,
텍스트만으로 중복 판정을 하면 다른 좌석의 행동을 놓치게 된다.
"""
from collections import deque


class LineDiffTracker:
    def __init__(self, history_size: int = 150, bucket: int = 40):
        self.bucket = bucket
        self._seen: deque = deque(maxlen=history_size)
        self._seen_set: set = set()

    def _key(self, line) -> tuple:
        bx = round(line.x / self.bucket)
        by = round(line.y / self.bucket)
        return (line.text.strip(), bx, by)

    def _remember(self, key) -> None:
        if len(self._seen) == self._seen.maxlen:
            oldest = self._seen[0]
            if self._seen.count(oldest) <= 1:
                self._seen_set.discard(oldest)
        self._seen.append(key)
        self._seen_set.add(key)

    def update(self, lines: list) -> list:
        """lines: OcrLine(text, x, y) 목록. 새로 나타난 OcrLine들만 반환한다."""
        new_lines = []
        for line in lines:
            if not line.text.strip():
                continue
            key = self._key(line)
            if key in self._seen_set:
                continue
            new_lines.append(line)
            self._remember(key)
        return new_lines

    def reset(self) -> None:
        self._seen.clear()
        self._seen_set.clear()
