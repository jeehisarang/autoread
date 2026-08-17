"""캡처 -> OCR -> 새 텍스트 추출 -> 행동 판별/필터링 -> 포지션 매칭 -> 엑셀 저장을
반복하는 백그라운드 스레드.

화면(또는 지정한 넓은 영역) 전체를 계속 OCR한 뒤, "행동 키워드(Fold/Call/Raise/
All-in 등)"에 해당하는 텍스트만 내용 기반으로 걸러서 기록한다. 플레이어 닉네임
대신 포지션(UTG/HJ/CO/BTN/SB/BB)으로 표시하며, 내 자리(seat_mapper 참고)의
행동은 별도 태그("hero_action")로 구분해서 화면에 색을 다르게 표시할 수 있게 한다.
"""
import threading
import time
from datetime import datetime

from . import action_parser, ocr_engine, window_capture
from .action_parser import DEFAULT_STREET, LOGGED_ACTION_TYPES
from .player_tracker import PlayerRegistry
from .seat_mapper import SeatMapper
from .text_diff import LineDiffTracker
from .xlsx_writer import XlsxLogger


def _format_action_line(position: str, action_type: str, amount: str) -> str:
    text = f"{position:<3} ] {action_type}"
    if amount:
        text += f" {amount}"
    return text


class Recorder:
    def __init__(
        self,
        hwnd: int,
        region: tuple[int, int, int, int] | None,
        xlsx_logger: XlsxLogger,
        interval_sec: float = 0.8,
        ocr_lang: str = "ko",
        on_line=None,  # callback(text:str, tag:str)  tag in hand_header/street_header/action/hero_action/result
        on_status=None,  # callback(status_text)
    ):
        self.hwnd = hwnd
        self.region = region
        self.xlsx_logger = xlsx_logger
        self.interval_sec = interval_sec
        self.ocr_lang = ocr_lang
        self.on_line = on_line
        self.on_status = on_status

        self._diff = LineDiffTracker()
        self._players = PlayerRegistry()
        self._seats = SeatMapper()
        self._hand_no = 1
        self._street = DEFAULT_STREET
        self._state_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self.event_count = 0

    def next_hand(self) -> int:
        """사용자가 수동으로 '다음 핸드' 버튼을 눌렀을 때 호출한다."""
        hand_no = self._advance_hand()
        self._emit_hand_start(hand_no)
        return hand_no

    def start(self) -> None:
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit_status(self, text: str) -> None:
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _emit_line(self, text: str, tag: str) -> None:
        if self.on_line:
            try:
                self.on_line(text, tag)
            except Exception:
                pass

    def _advance_hand(self) -> int:
        with self._state_lock:
            self._hand_no += 1
            self._street = DEFAULT_STREET
            return self._hand_no

    def _emit_hand_start(self, hand_no: int) -> None:
        self._emit_line(f"[{hand_no}게임 시작]", "hand_header")
        self._emit_line(f"[{DEFAULT_STREET}]", "street_header")

    def _run(self) -> None:
        self._emit_status("기록을 시작합니다...")
        with self._state_lock:
            hand_no = self._hand_no
        self._emit_hand_start(hand_no)

        consecutive_capture_fail = 0
        while not self._stop_flag.is_set():
            start_t = time.time()
            img = window_capture.capture_window(self.hwnd)
            if img is None:
                consecutive_capture_fail += 1
                if consecutive_capture_fail % 5 == 1:
                    self._emit_status("창을 캡처할 수 없습니다. 창이 최소화되지 않았는지 확인하세요.")
                self._sleep_remaining(start_t)
                continue
            consecutive_capture_fail = 0

            if self.region:
                x1, y1, x2, y2 = self.region
                try:
                    img = img.crop((x1, y1, x2, y2))
                except Exception:
                    pass

            capture_w, capture_h = img.size
            lines = ocr_engine.recognize_lines(img, lang=self.ocr_lang)

            # 닉네임 위치는 화면에 계속 보이는 것들이므로, 매 프레임 전체 인식 결과로 갱신한다.
            self._players.update(lines)
            if not self._seats.is_locked():
                self._seats.try_lock(self._players, capture_w, capture_h)

            new_lines = self._diff.update(lines)
            for line in new_lines:
                self._handle_line(line)

            if not new_lines:
                self._emit_status(f"기록 중... (총 {self.event_count}건 기록됨)")

            self._sleep_remaining(start_t)

        self._emit_status("기록이 중지되었습니다.")

    def _resolve_position(self, parsed, line):
        x, y = line.x, line.y
        if parsed.player:
            entry = next((e for e in self._players.entries() if e.name == parsed.player), None)
            if entry is not None:
                x, y = entry.x, entry.y
        with self._state_lock:
            hand_no = self._hand_no
        return self._seats.position_for(x, y, hand_no)

    def _handle_line(self, line) -> None:
        parsed = action_parser.parse_line(line.text)

        if parsed.kind == "new_hand":
            hand_no = self._advance_hand()
            self._emit_hand_start(hand_no)
            return

        if parsed.kind == "street":
            with self._state_lock:
                changed = self._street != parsed.street_name
                if changed:
                    self._street = parsed.street_name
                street = self._street
            if changed:
                self._emit_line(f"[{street}]", "street_header")
            return

        if parsed.kind == "result":
            position, _ = self._resolve_position(parsed, line)
            with self._state_lock:
                hand_no = self._hand_no
            self._emit_line(f"[{hand_no}게임 결과] {position or '?'} 승", "result")
            return

        if parsed.kind != "action" or parsed.action_type not in LOGGED_ACTION_TYPES:
            return  # 행동으로 분류되지 않은 텍스트(UI/채팅/골드 등)는 기록하지 않는다.

        position, is_hero = self._resolve_position(parsed, line)
        position_label = position or "?"

        with self._state_lock:
            hand_no = self._hand_no
            street = self._street

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp, hand_no, street, position_label, parsed.action_type,
            parsed.amount, "예" if is_hero else "", parsed.raw_text,
        ]
        ok = self.xlsx_logger.append(row, highlight=is_hero)
        self.event_count += 1
        if not ok:
            self._emit_status(self.xlsx_logger.last_error or "저장 실패")

        display_line = _format_action_line(position_label, parsed.action_type, parsed.amount)
        self._emit_line(display_line, "hero_action" if is_hero else "action")

    def _sleep_remaining(self, start_t: float) -> None:
        elapsed = time.time() - start_t
        remaining = self.interval_sec - elapsed
        if remaining > 0:
            self._stop_flag.wait(remaining)
