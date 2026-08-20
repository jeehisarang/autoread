"""캡처 -> (좌석별) 고정 영역 OCR -> 행동 판별 -> 딜러 기준 포지션 계산 -> 엑셀 저장을
반복하는 백그라운드 스레드.

task.md 3절 지침에 따라 전체 화면을 OCR하지 않는다. 매 프레임 창을 한 번 캡처한
뒤, table_layout.json에 정의된 좌석별 고정 영역만 잘라서 OCR하고, 보드 영역은
OCR 대신 카드 장수만 픽셀 밝기로 판별한다(board_reader 참고, 카드 랭크 폰트가
장식체라 OCR로 읽히지 않는 것을 실측 확인했다).
"""
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import action_parser, ai_seat_detector, board_reader, dealer_position, ocr_engine, window_capture
from .action_parser import DEFAULT_STREET, LOGGED_ACTION_TYPES
from .table_layout import TableLayout, load_layout, resolve_ratio_region
from .text_diff import LineDiffTracker
from .xlsx_writer import XlsxLogger


# task.md "결과물 출력 형식": 딜러 기준 오프셋(0=딜러 자신)을 표준 포지션 이름으로
# 표시한다. dealer_position.py의 인식/계산 로직(오프셋 계산)은 그대로 두고, 여기서는
# 그 결과 문자열("딜러"/"딜러+N")을 화면·엑셀에 보여줄 이름으로만 바꾼다 - 표준
# 이름이 없는 인원수(여기 표에 없는 크기)거나 딜러를 아직 못 찾았으면("?") 원래
# 라벨을 그대로 쓴다.
POSITION_NAMES_BY_TABLE_SIZE = {
    2: ["BTN", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["BTN", "SB", "BB", "UTG"],
    5: ["BTN", "SB", "BB", "UTG", "CO"],
    6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
    7: ["BTN", "SB", "BB", "UTG", "UTG+1", "HJ", "CO"],
    8: ["BTN", "SB", "BB", "UTG", "UTG+1", "MP", "HJ", "CO"],
    9: ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"],
}

# task.md "결과 형식 히어로 중심 개선": 내 자리는 다른 사람과 다른 구분자(":")로
# 표시해서 눈에/귀에 더 잘 띄게 한다(이전엔 "★ 나"로 자리 이름만 바꿨었는데, 이번
# 지시서 예시는 구분자까지 다르게("나   : Fold") 요구한다).
HERO_DISPLAY_LABEL = "나"


def _format_action_line(position: str, action_type: str, amount: str) -> str:
    text = f"{position:<5} ] {action_type}"
    if amount:
        text += f" {amount}"
    return text


def _format_hero_line(action_type: str, amount: str = "") -> str:
    text = f"{HERO_DISPLAY_LABEL:<5}: {action_type}"
    if amount:
        text += f" {amount}"
    return text


class Recorder:
    def __init__(
        self,
        hwnd: int,
        xlsx_logger: XlsxLogger,
        interval_sec: float = 0.8,
        ocr_lang: str = "ko",
        layout: TableLayout | None = None,
        on_line=None,  # callback(text:str, tag:str)  tag in hand_header/street_header/action/hero_action/result
        on_status=None,  # callback(status_text)
    ):
        self.hwnd = hwnd
        self.xlsx_logger = xlsx_logger
        self.interval_sec = interval_sec
        self.ocr_lang = ocr_lang
        self.layout = layout or load_layout()
        self.on_line = on_line
        self.on_status = on_status

        self._seat_diffs = {seat.index: LineDiffTracker() for seat in self.layout.seats}
        self._result_diff = LineDiffTracker()
        self._win_amount_diffs = {seat.index: LineDiffTracker() for seat in self.layout.seats}
        self._reported_win_hand_by_seat: dict[int, int] = {}  # seat_index -> 이미 보고한 hand_no
        self._last_action_by_seat: dict[int, tuple] = {}  # seat_index -> (street, action_type, amount)
        # 밝기 기반 Fold 판정용 상태(task.md "Fold 밝기 감지"). "겹" OCR에 기대지 않고,
        # 좌석이 어두워지는 것 자체로 판정한다(아래 _check_fold_by_brightness 참고).
        self._seat_brightness_baseline: dict[int, float] = {}  # seat_index -> 최근 "밝을 때" 밝기
        self._seat_dark_streak: dict[int, int] = {}  # seat_index -> 연속으로 어두웠던 프레임 수
        self._folded_seats_this_hand: set[int] = set()  # 이번 핸드에 이미 폴드로 확정한 좌석
        # 베팅 금액 재시도용(task.md "베팅 금액 보완"). seat_index -> {row, hand_no, street, ...}
        self._pending_amount: dict[int, dict] = {}
        # 턴(파란 아우라) 감지용 상태(task.md/task2.md "턴 감지 우선 구조").
        self._seat_blue_baseline: dict[int, float] = {}  # seat_index -> 평소 파랑 비율
        self._seat_aura_recent_frames: dict[int, int] = {}  # seat_index -> 마지막으로 아우라 본 뒤 지난 프레임 수
        self._current_actor: int | None = None
        # task.md "AI 하이브리드 도입" - 핸드당 1회만 AI(OpenAI vision)를 불러서
        # 딜러/히어로/홀카드를 고정하고, 그 다음부터는 로컬 로직만 쓴다.
        self._ai_call_done_this_hand = False
        self._hero_seat_override: int | None = None  # AI가 알려준 이번 핸드 히어로 좌석
        self._hero_cards = ""  # AI가 읽은 히어로 홀카드(예: "As Kh")
        self._hand_start_frame_count = 0  # 새 핸드 시작 후 지난 프레임 수(카드 배분 대기)
        # 히어로가 폴드하면 이 핸드는 다음 핸드 시작 전까지 상세 분석을 멈춘다.
        self._hero_folded_this_hand = False
        # task.md "히어로 (대기) 표시 안정화": 같은 턴(같은 스트리트, 아직 행동 전)에
        # "나 : (대기)"를 두 번 찍지 않기 위한 명시적 플래그.
        self._hero_wait_announced_this_turn = False
        self._dealer_seat: int | None = None
        self._dealer_candidate: int | None = None  # "다음 핸드는 이 자리일 것" 예측(딜러+1)
        self._dealer_recheck_needed = True  # 시작 직후에는 한 번 찾아야 하므로 True
        self._turn_seat_index: int | None = None  # 지금 턴일 거라고 예측하는 좌석
        self._seat_fingerprints: dict[int, bytes] = {}  # 좌석별 직전 프레임 픽셀 지문
        self._frame_count = 0
        # task.md "동기화 단계 도입": 동기화 이후 실제로 감지되는 첫 새 핸드가
        # 1번이 되도록 0에서 시작한다(예전엔 시작하자마자 무조건 "[1게임 시작]"을
        # 찍었는데, 이제는 _update_street의 새 핸드 감지 분기에서만 올린다).
        self._hand_no = 0
        # 동기화가 끝나고 "다음 새 핸드"가 실제로 감지되기 전까지는 기록을 쉰다
        # (지금 진행 중인 핸드는 무시 - task.md 4절 "현재 진행 중인 핸드는 무시한다").
        self._recording_active = False
        self._just_entered_new_hand = False
        # task.md "동기화 단계 도입" 6절: 동기화가 성공하면 딜러/히어로는 "고정된
        # 값"으로 계속 쓰고 매 핸드 AI를 다시 부르지 않는다 - _process_frame에서
        # 이 플래그를 보고 _maybe_call_ai_for_hand 자동 호출을 건너뛴다.
        self._synced_successfully = False
        # 동기화 때 얻은 인원수(있으면 이걸 [N게임 시작] (M인 테이블) 표기에도 그대로
        # 쓴다 - task.md "새 핸드 감지 로직 보완" 5절, 인원수 표기 통일).
        self._active_seat_count: int | None = None
        # task.md "새 핸드 감지 로직 보완": 프리플랍 폴드아웃(보드가 0장에서 안
        # 바뀌고 끝나는 핸드)은 보드 장수 변화로 새 핸드를 못 잡는다 - 대신 "이번
        # 핸드에 결과가 이미 찍혔는데 보드가 계속 0장"인 상태에서 딜러 버튼이 실제로
        # 옮겨갔는지 직접 확인해서 잡는다(_check_dealer_moved_new_hand 참고).
        self._hand_result_seen = False
        self._frames_since_result = 0
        self._pending_new_dealer: int | None = None
        self._pending_new_dealer_streak = 0
        self._ai_new_hand_check_done = False
        self._street = DEFAULT_STREET
        self._board_count = 0
        self._pending_board_count: int | None = None  # 스트리트 전환 확정 대기중인 값
        self._pending_board_count_streak = 0
        self._capture_size = (1, 1)  # 첫 프레임 처리 전까지만 쓰이는 자리표시자
        self._state_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self.event_count = 0

        # 좌석 5곳 + 결과 패널의 OCR을 병렬로 돌린다. 실측 결과 winocr 호출을
        # 순차로 하면 0.66초, 스레드풀로 동시에 돌리면 0.24초로 거의 3배 빨라졌다
        # (OCR 엔진이 동시 요청을 잘 처리함) - 체크/다이처럼 짧게 떴다 사라지는
        # 행동을 놓치지 않으려면 이 정도 폴링 속도가 필요하다.
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ocr")

    # 딜러 위치는 매 프레임 다시 찾지 않는다(찾는 동안 행동 인식이 늦어지는 문제가 있었음).
    # 새 핸드 시작 시점, 또는 이 프레임 수마다 한 번만 재확인한다(이미 찾은 상태의
    # 주기적 안전점검용).
    DEALER_RECHECK_EVERY_FRAMES = 45

    # 딜러를 아직 한 번도 못 찾았을 때는 위보다 짧은 주기로 재시도한다(그래도 매
    # 프레임은 아니다 - 화면에 DEALER 표시가 아예 없는 동안 매 프레임 전체 좌석
    # 스캔을 반복하면서 계속 느려지는 버그가 있었다).
    DEALER_INITIAL_SEARCH_RETRY_FRAMES = 10

    # 결과 패널("내기록")은 줄 수가 많아서 OCR 자체가 훨씬 느리다(실측: 좌석 5곳 병렬
    # 20~50ms인데 결과패널 하나만 180~320ms, 같이 넣으면 배치 전체가 그 느린 호출을
    # 기다리느라 0.3~1초까지 늘어남). 승리 표시는 핸드당 한 번뿐이라 매 프레임 볼
    # 필요가 없으므로, 행동 감지 핫패스에서 빼고 이 프레임 수마다 한 번만 확인한다.
    RESULT_PANEL_CHECK_EVERY_FRAMES = 10

    # 포지션/행동 태그가 좌석 박스 어디에 뜨는지는 게임마다 다르다(한게임=위쪽,
    # ClubWPT GOLD=아래쪽, 코인포커=좌상단 겹침). 매번 하드코딩하는 대신
    # table_layout.json의 action_zone_bottom_ratio(아래쪽부터 몇 %를 볼지, 1.0=전체)
    # 로 게임별로 조정한다 - 기본값 0.35는 ClubWPT GOLD 기준 그대로 유지된다.
    # 짧은 글자("겹" 등)는 주변 텍스트가 있어야 OCR이 더 잘 잡는 경향이 실측 확인돼서,
    # 코인포커는 이 값을 1.0(박스 전체)으로 설정해뒀다.

    # 딜러 버튼("D" 표시)은 좌석 박스 "안"이 아니라 바로 바깥, 테이블 안쪽(중심)
    # 방향으로 상당히 떨어진 자리에 뜬다(오늘 실전 창 직접 캡처로 재실측 - 이전에
    # 정해둔 균일한 15px 위아래 패딩으로는 안 닿아서 "계속 찾는 중"에 머물러 있었다).
    # 좌석 바깥쪽(테이블 중심 반대 방향)은 기존처럼 작게, 중심 쪽은 훨씬 넉넉하게
    # 비대칭으로 확장한다 - 균일하게 다 키우면 다른 좌석들의 카드 뒷면 무늬(빨간
    # 다이아몬드 패턴)까지 걸려서 오탐이 난다(실측 확인).
    DEALER_MARKER_PAD_X = 90 / 1030
    # (실측 중 발견: 바깥쪽을 너무 좁게 주면(15px) 마커가 안쪽에 다 들어와 있어도
    # 원 검출 자체가 이상하게 실패했다 - 크롭 안에 위쪽 여백이 어느 정도 있어야
    # HoughCircles가 안정적으로 원을 찾는 것으로 보인다. 30px부터 안정적으로 성공.)
    DEALER_MARKER_PAD_Y_OUTER = 30 / 720   # 테이블 중심 반대쪽 - 좁게(카드 뒷면 오탐 방지)
    DEALER_MARKER_PAD_Y_INNER = 55 / 720   # 테이블 중심쪽 - 넓게(실측: 마커가 여기 있음)

    def _action_zone(self, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = region
        ratio = self.layout.action_zone_bottom_ratio
        return (x1, y2 - (y2 - y1) * ratio, x2, y2)

    def _dealer_zone(self, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = region
        y1 = y1 + self.layout.dealer_zone_top_trim
        bx1, by1, bx2, by2 = self.layout.board_region
        board_cy = (by1 + by2) / 2
        seat_cy = (y1 + y2) / 2
        if seat_cy < board_cy:
            # 위쪽 줄 좌석 - 테이블 중심은 "아래쪽"이니 아래를 넉넉하게
            top_pad, bottom_pad = self.DEALER_MARKER_PAD_Y_OUTER, self.DEALER_MARKER_PAD_Y_INNER
        else:
            # 아래쪽 줄 좌석 - 테이블 중심은 "위쪽"이니 위를 넉넉하게
            top_pad, bottom_pad = self.DEALER_MARKER_PAD_Y_INNER, self.DEALER_MARKER_PAD_Y_OUTER
        return (
            x1 - self.DEALER_MARKER_PAD_X, y1 - top_pad,
            x2 + self.DEALER_MARKER_PAD_X, y2 + bottom_pad,
        )

    def _result_zone(self, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """좌석 박스 위쪽으로 result_zone_top_extend(창 높이 비율)만큼 늘린 영역 -
        코인포커에서 이긴 좌석의 홀카드 위에 뜨는 "+금액"을 잡기 위해서다(위 필드
        설명 참고)."""
        x1, y1, x2, y2 = region
        return (x1, y1 - self.layout.result_zone_top_extend, x2, y2)

    # 베팅(레이즈/콜/벳) 금액은 좌석 박스 "안"이 아니라, 좌석과 보드 사이에 칩 아이콘과
    # 함께 별도로 뜬다(실측 확인: coinpoker_samples 영상). 좌석마다 새 좌표를 재는 대신,
    # 이미 있는 좌석 박스 중심에서 board_region 중심 방향으로 일정 비율만큼 이동한
    # 지점을 계산해서 잡는다 - 여러 좌석(상/중/하, 좌/우, 히어로)에서 실측으로 맞는
    # 것을 확인했다. 베팅이 없는 프레임엔 빈 칸이라 그냥 무시된다. 폭/높이는 1030x720
    # 실측 기준 150px/70px을 창 너비/높이 비율로 환산.
    BET_ZONE_FRAC = 0.4
    BET_ZONE_WIDTH = 150 / 1030
    BET_ZONE_HEIGHT = 70 / 720

    def _bet_zone(self, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = region
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        bx1, by1, bx2, by2 = self.layout.board_region
        board_cx, board_cy = (bx1 + bx2) / 2, (by1 + by2) / 2
        px = cx + (board_cx - cx) * self.BET_ZONE_FRAC
        py = cy + (board_cy - cy) * self.BET_ZONE_FRAC
        half_w, half_h = self.BET_ZONE_WIDTH / 2, self.BET_ZONE_HEIGHT / 2
        return (px - half_w, py - half_h, px + half_w, py + half_h)

    def _read_bet_zone_amount(self, seat_index: int, img) -> str:
        seat = self.layout.seat_by_index(seat_index)
        if seat is None:
            return ""
        region = self._scaled(self._bet_zone(seat.region))
        lines = self._ocr_lines_safe(self._safe_crop(img, region))
        for line in lines:
            amount = action_parser.extract_amount_from_text(line.text)
            if amount:
                return amount
        return ""

    # task.md "베팅 금액 보완": 행동이 감지된 그 순간 한 번만 베팅액을 읽으면, 그
    # 한 프레임에서 OCR이 잘못 읽으면(실측 확인: "2,000"을 "기000"으로 오인식)
    # 그걸로 끝이었다. 칩 숫자는 보통 몇 초간 화면에 남아있으므로, 그동안 몇 프레임
    # 더 재시도할 기회를 준다. 최대 대기 프레임 수(실제 0.2초 주기 기준 약 3초).
    PENDING_AMOUNT_MAX_FRAMES = 15

    def _check_pending_amounts(self, img) -> None:
        if not self._pending_amount:
            return
        with self._state_lock:
            hand_no, street = self._hand_no, self._street
        done = []
        for seat_index, info in self._pending_amount.items():
            if info["hand_no"] != hand_no or info["street"] != street:
                done.append(seat_index)  # 핸드/스트리트가 넘어감 - 더 기다려도 의미 없음
                continue
            amount = self._read_bet_zone_amount(seat_index, img)
            if amount:
                self.xlsx_logger.update_amount(info["row"], amount)
                if info["is_hero"]:
                    corrected = _format_hero_line(info["action_type"], amount)
                else:
                    corrected = _format_action_line(info["position"], info["action_type"], amount)
                self._emit_line(corrected, "hero_action" if info["is_hero"] else "action")
                done.append(seat_index)
                continue
            info["frames_left"] -= 1
            if info["frames_left"] <= 0:
                done.append(seat_index)  # 끝까지 못 찾음 - 억지로 채우지 않고 그냥 포기
        for seat_index in done:
            self._pending_amount.pop(seat_index, None)

    # task.md/task2.md "턴(아우라) 감지 우선 구조" - 실제 켜져 있는 코인포커 창을
    # 직접 캡처해서 실측(합성 아님): 지금 턴인 좌석은 아바타 "위쪽 여백"(아바타
    # 원 자체가 아니라 그 바깥, 어두운 배경 쪽으로 번지는 부분)에 파란색 계열
    # 링이 뜬다. 아바타 그림 자체가 원래 푸른 톤인 좌석(예: 실측 중 만난 여우
    # 아바타)도 있어서, 아바타 "안쪽"까지 포함해서 보면 그런 좌석이 오탐된다 -
    # 그래서 아바타 원 바깥, 박스 위쪽 여백 띠만 본다(아래 _aura_zone). 승자
    # 표시(황금 아우라)도 실측해보니 같은 위치(아바타 위쪽)에 뜨는 걸 확인해서
    # 같은 영역을 재사용한다. 색만 다르게 본다(파랑 vs 골드).
    AURA_ZONE_PAD_X = 0.03
    AURA_ZONE_TOP_FAR = 0.16   # 박스 위쪽 이만큼(비율)부터
    AURA_ZONE_TOP_NEAR = 0.02  # 박스 바로 위 이만큼은 제외(이름표 자체의 색과 안 섞이게)
    AURA_BLUE_PIXEL_THRESHOLD = 25   # 픽셀 하나가 "파랗다"고 볼 B-max(R,G) 문턱값
    AURA_GOLD_PIXEL_THRESHOLD = 30   # 픽셀 하나가 "골드"라고 볼 min(R,G)-B 문턱값(+R>120)
    # 아바타 자체가 원래 좀 푸른 좌석도 있어서(실측 확인), 절대 기준이 아니라
    # "이 좌석 평소보다 이만큼 더 파래졌는지"로 판정한다(task.md "좌석마다
    # baseline을 따로 두어 조명/스킨 차이에 대응" 지침).
    AURA_BLUE_SPIKE_MARGIN = 0.012
    AURA_GOLD_MIN_RATIO = 0.015  # 골드는 평소 거의 0%라 절대 기준으로도 안전(실측 확인)
    AURA_HISTORY_FRAMES = 8  # 최근 아우라를 "기억"하는 길이(0.2초 주기 기준 약 1.6초)
    AURA_GLOBAL_EVENT_SEAT_RATIO = 0.5  # 절반 넘는 좌석이 동시에 신호를 내면 화면 전환으로 보고 무시

    def _aura_zone(self, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = region
        return (x1 - self.AURA_ZONE_PAD_X, y1 - self.AURA_ZONE_TOP_FAR, x2 + self.AURA_ZONE_PAD_X, y1 - self.AURA_ZONE_TOP_NEAR)

    @staticmethod
    def _aura_color_ratio(crop, mode: str) -> float | None:
        if crop is None or crop.width <= 0 or crop.height <= 0:
            return None
        try:
            pixels = list(crop.convert("RGB").resize((24, 18)).getdata())
        except Exception:
            return None
        if not pixels:
            return None
        count = 0
        for r, g, b in pixels:
            if mode == "blue":
                if b - max(r, g) > Recorder.AURA_BLUE_PIXEL_THRESHOLD:
                    count += 1
            else:
                if min(r, g) - b > Recorder.AURA_GOLD_PIXEL_THRESHOLD and r > 120:
                    count += 1
        return count / len(pixels)

    def _seat_has_gold_aura(self, seat_index: int, img) -> bool:
        seat = self.layout.seat_by_index(seat_index)
        if seat is None:
            return False
        crop = self._safe_crop(img, self._scaled(self._aura_zone(seat.region)))
        ratio = self._aura_color_ratio(crop, "gold")
        return ratio is not None and ratio > self.AURA_GOLD_MIN_RATIO

    def _check_turn_aura(self, img) -> None:
        """좌석마다 파란 아우라 후보인지 보고, current_actor(_turn_seat_index)를
        실제 시각 신호로 갱신한다. 규칙 기반 예측(_next_in_rotation)이 어긋나도
        이 신호가 실제 턴을 다시 잡아준다."""
        candidates = []
        watch_count = 0
        for seat in self.layout.seats:
            watch_count += 1
            crop = self._safe_crop(img, self._scaled(self._aura_zone(seat.region)))
            ratio = self._aura_color_ratio(crop, "blue")
            if ratio is None:
                continue
            baseline = self._seat_blue_baseline.get(seat.index)
            if baseline is None:
                self._seat_blue_baseline[seat.index] = ratio
                continue
            if ratio > baseline + self.AURA_BLUE_SPIKE_MARGIN:
                candidates.append(seat.index)
            else:
                self._seat_blue_baseline[seat.index] = ratio  # 서서히 조명 변화에 맞춰간다

        is_global_event = watch_count > 0 and len(candidates) > watch_count * self.AURA_GLOBAL_EVENT_SEAT_RATIO
        if is_global_event:
            candidates = []  # 화면 전체가 바뀐 프레임으로 보고 이번 프레임 신호는 버린다

        for seat in self.layout.seats:
            if seat.index in candidates:
                self._seat_aura_recent_frames[seat.index] = 0  # 이번 프레임에 봤음
            else:
                prev = self._seat_aura_recent_frames.get(seat.index)
                if prev is not None:
                    self._seat_aura_recent_frames[seat.index] = prev + 1

        # "최근 AURA_HISTORY_FRAMES 이내에 아우라를 본" 좌석 중 가장 최근인 좌석이
        # current_actor다 - 아우라가 아주 짧게(빠른 폴드) 사라져도 이 히스토리
        # 덕분에 놓치지 않는다(task.md "빠른 폴드 케이스" 지침).
        recent = [
            (age, idx) for idx, age in self._seat_aura_recent_frames.items()
            if age <= self.AURA_HISTORY_FRAMES
        ]
        if not recent:
            return
        recent.sort()
        newest_seat = recent[0][1]
        if newest_seat != self._current_actor:
            self._current_actor = newest_seat
            self._turn_seat_index = newest_seat
            # task.md "결과 형식 히어로 중심 개선" 3절: 내 차례가 되면(아우라가 내
            # 자리로 옮겨오면) 실제 행동이 잡히기 전에 "나   : (대기)"를 먼저 찍는다.
            # 단, 아우라가 잠깐 흔들려서(다른 좌석으로 갔다가 곧바로 돌아오는 등)
            # current_actor가 짧게 왔다갔다해도 같은 턴이면 두 번 찍지 않는다
            # (task.md "위치 인식 실패 수정" 4절 - 명시적 플래그로 막는다).
            if newest_seat == self._hero_seat_index() and not self._hero_wait_announced_this_turn:
                self._emit_line(_format_hero_line("(대기)"), "hero_action")
                self._hero_wait_announced_this_turn = True

    # "겹"(Fold) 글자는 고립된 한 글자라 winocr이 원리적으로 못 읽는다(실측 확인,
    # 딜러 버튼 글자를 픽셀로 판별하게 된 이유와 같은 문제). 대신 폴드하면 좌석
    # 전체가 눈에 띄게 어두워지고 그 상태가 유지되는 걸 실측으로 확인해서(같은
    # 좌석의 폴드 전후 평균 밝기가 51 -> 16 수준으로, 그리고 그 뒤로 계속 어둡게
    # 유지됨), OCR 대신 밝기 급락 + 유지로 폴드를 판정한다.
    FOLD_BRIGHTNESS_DROP_RATIO = 0.75  # 기준 밝기의 75% 밑으로 떨어지면 "어두워짐" 후보
    FOLD_BRIGHTNESS_CONFIRM_FRAMES = 3  # 순간 깜빡임이 아니라 이만큼 연속으로 어두워야 확정
    # 실측 영상으로 검증하다가, 화면 전체(창 포커스 변화/알림 등)가 한꺼번에 어두워지는
    # 프레임을 실제로 봤다 - 그 프레임에서는 폴드 안 한 좌석까지 전부 "어두워짐 후보"에
    # 걸렸다. 좌석 절반 넘게 동시에 어두워지면 "이 좌석이 폴드해서"가 아니라 "화면 전체가
    # 바뀐 것"으로 보고, 그 프레임은 아무 좌석의 연속 카운트도 올리지 않는다(개별 폴드는
    # 이렇게 여러 좌석이 같은 프레임에 한꺼번에 어두워지지 않는다 - 실측 확인).
    FOLD_GLOBAL_DIM_SEAT_RATIO = 0.5

    @staticmethod
    def _mean_brightness(crop) -> float | None:
        if crop is None or crop.width <= 0 or crop.height <= 0:
            return None
        try:
            gray = crop.convert("L")
            pixels = gray.resize((16, 16)).tobytes()  # 평균만 필요하므로 작게 축소해서 빠르게
            return sum(pixels) / len(pixels)
        except Exception:
            return None

    def _check_fold_by_brightness(self, img) -> None:
        candidates: list[tuple] = []  # [(seat, brightness), ...] 이번 프레임에 "어두워짐 후보"인 좌석
        watch_count = 0
        for seat in self.layout.seats:
            if seat.index in self._folded_seats_this_hand:
                continue  # 이번 핸드에 이미 폴드로 확정됨 - 계속 어두운 게 당연하니 재확인 안 함
            watch_count += 1
            crop = self._safe_crop(img, self._scaled(seat.region))
            brightness = self._mean_brightness(crop)
            if brightness is None:
                continue
            baseline = self._seat_brightness_baseline.get(seat.index)
            if baseline is None:
                self._seat_brightness_baseline[seat.index] = brightness
                continue
            if brightness < baseline * self.FOLD_BRIGHTNESS_DROP_RATIO:
                candidates.append((seat, brightness))
            else:
                # 다시 밝아졌다 -> 폴드가 아니라 순간적인 화면 변화(애니메이션 등)였던
                # 것으로 보고, 기준 밝기를 갱신하며 연속 카운트를 리셋한다. 원래 항상
                # 어둡던 좌석(빈 자리 등)은 baseline 자체가 낮게 잡히므로 여기서
                # 오탐이 나지 않는다(급격한 "하락"이 없으면 애초에 후보가 안 됨).
                self._seat_brightness_baseline[seat.index] = brightness
                self._seat_dark_streak[seat.index] = 0

        if watch_count > 0 and len(candidates) > watch_count * self.FOLD_GLOBAL_DIM_SEAT_RATIO:
            return  # 화면 전체가 어두워진 프레임으로 보고 이번 프레임은 건너뛴다

        for seat, brightness in candidates:
            streak = self._seat_dark_streak.get(seat.index, 0) + 1
            self._seat_dark_streak[seat.index] = streak
            if streak >= self.FOLD_BRIGHTNESS_CONFIRM_FRAMES:
                self._folded_seats_this_hand.add(seat.index)
                fold_line = ocr_engine.OcrLine(text="FOLD", x=0, y=0)
                self._handle_seat_line(seat.index, fold_line, img)

    # 홀덤 행동 순서 규칙 (task.md 1단계):
    # - 프리플랍: 딜러(BTN) 기준 3자리 다음(SB, BB 다음 자리 = UTG)부터 시작해서
    #   블라인드를 낸 BB가 마지막에 행동한다.
    # - 포스트플랍(플랍/턴/리버): 매 스트리트마다 딜러 다음 자리(SB)부터 시작해서
    #   딜러(BTN)가 마지막에 행동한다.
    # 이 오프셋(프리플랍 +3, 포스트플랍 +1)은 인원수와 무관하게 항상 성립한다 -
    # UTG는 "블라인드 두 명 다음 자리"라는 정의 자체가 6인이든 8인이든 동일하기
    # 때문이다. (6인 기준 전체 순서: BTN→SB→BB→UTG→HJ→CO)
    PREFLOP_START_OFFSET = 3  # UTG
    POSTFLOP_START_OFFSET = 1  # SB

    def _seat_at_offset(self, dealer_index: int, offset: int) -> int:
        """딜러 좌석 기준 시계방향으로 offset칸 다음 좌석을 반환한다."""
        order = self.layout.clockwise_order
        i = order.index(dealer_index)
        return order[(i + offset) % len(order)]

    def _next_in_rotation(self, seat_index: int) -> int:
        """seat_index 바로 다음 좌석(시계방향 한 칸). 실제 행동한 좌석을 기준으로
        다음 턴을 예측할 때 쓴다(자가 교정) - 여기서는 스트리트 시작 규칙이 아니라
        "누가 방금 행동했으니 다음은 그 옆"이라는 순수한 이웃 관계만 본다."""
        return self._seat_at_offset(seat_index, 1)

    @staticmethod
    def _fingerprint(crop) -> bytes:
        """아주 작게 축소해서 바이트로 비교하는 빠른 픽셀 지문. 완벽한 해시일 필요
        없이 "이번 프레임에 뭔가 바뀌었는지"만 빠르게 판별하면 된다. 실측 결과
        OCR 1회(약 30ms)보다 약 230배 빠르다(약 0.13ms)."""
        if crop is None:
            return b""
        try:
            return crop.convert("L").resize((16, 8)).tobytes()
        except Exception:
            return b""

    def _seats_to_watch(self, img) -> list:
        """이번 프레임에 OCR할 좌석 목록.
        - 턴 좌석(다음에 행동할 거라 예측하는 자리)은 매 프레임 무조건 본다.
        - 나머지 좌석은 픽셀 지문 비교로 "이번 프레임에 실제로 뭔가 바뀌었는지"를
          5곳 전부 거의 공짜로 확인하고, 바뀐 곳만 OCR한다. 라운드로빈으로 몇 곳만
          돌아가며 보던 이전 방식과 달리, 매 프레임 전부 다 확인하므로 "다음 차례가
          올 때까지 기다리는" 커버리지 공백이 없다.
        - 딜러/턴을 아직 모르면 안전하게 5곳 전부 OCR한다.
        (한계: 두 캡처 사이에 행동 배지가 떴다가 완전히 사라지면 지문 비교로도
        못 잡는다 - 이건 폴링 주기 자체의 한계라 지문 비교로도 넘어설 수 없다.)
        """
        seats = self.layout.seats
        changed_or_new = []
        for seat in seats:
            region = self._scaled(self._action_zone(seat.region))
            crop = self._safe_crop(img, region)
            fp = self._fingerprint(crop)
            prev = self._seat_fingerprints.get(seat.index)
            if prev is None or fp != prev:
                changed_or_new.append(seat)
            self._seat_fingerprints[seat.index] = fp

        if self._turn_seat_index is None:
            return list(seats)  # 딜러/턴 미확정 -> 안전하게 전부 본다

        picked = []
        turn_seat = self.layout.seat_by_index(self._turn_seat_index)
        if turn_seat is not None:
            picked.append(turn_seat)
        for seat in changed_or_new:
            if seat not in picked:
                picked.append(seat)
        return picked

    def next_hand(self) -> int:
        """사용자가 수동으로 '다음 핸드' 버튼을 눌렀을 때 호출한다."""
        with self._state_lock:
            self._hand_no += 1
            self._street = DEFAULT_STREET
            self._board_count = 0
            hand_no = self._hand_no
        for tracker in self._seat_diffs.values():
            tracker.reset()
        self._last_action_by_seat.clear()
        self._dealer_recheck_needed = True  # 새 핸드 -> 딜러 위치 재확인 시점
        self._dealer_candidate = self._next_in_rotation(self._dealer_seat) if self._dealer_seat is not None else None
        # 새 핸드는 항상 프리플랍부터이므로 UTG(딜러+3)에서 턴을 시작한다.
        self._turn_seat_index = (
            self._seat_at_offset(self._dealer_seat, self.PREFLOP_START_OFFSET)
            if self._dealer_seat is not None else None
        )
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
        self._executor.shutdown(wait=False, cancel_futures=True)

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

    def _run(self) -> None:
        # task.md "기록 시작 시 동기화 단계 도입": 바로 기록을 쏟아내지 않고, 먼저
        # 딜러/히어로 위치를 확실하게 잡는 동기화 단계를 거친다. 지금 진행 중인
        # 핸드는 무시하고(성공/실패 상관없이), 다음 새 핸드가 시작될 때 비로소
        # 본 기록을 시작한다(_process_frame의 self._recording_active 게이트 참고).
        self._emit_status("초기 동기화 중... (딜러/위치 인식 중)")
        self._run_sync_phase()
        # 동기화가 끝난 시점에 마침 보드가 0장(프리플랍)이었다면, 그 값 그대로는
        # "새로 0이 됐다"는 변화가 아니라서 다음 실제 새 핸드를 못 잡을 수 있다
        # (-1은 실제로 나올 수 없는 값이라 다음 프레임의 진짜 판독값과는 항상
        # "달라짐"으로 잡혀서, 이번에 진행 중이던 핸드를 확실히 무시하게 만든다).
        self._board_count = -1
        self._pending_board_count = None
        self._pending_board_count_streak = 0
        if not self._stop_flag.is_set():
            self._emit_status("다음 핸드 시작을 기다리는 중...")

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

            self._process_frame(img)

            self._sleep_remaining(start_t)

        self._emit_status("기록이 중지되었습니다.")

    # 동기화 단계에서 AI를 최대 이만큼 부른다(1회로 부족하면 교차확인을 위해
    # 2~3회까지) - task.md "동기화 동안 확실하게 잡는다" 지침.
    SYNC_MAX_AI_CALLS = 3
    # AI 호출이 느릴 수 있어(실측: 2~10초) 넉넉하게 잡되, 무한 대기는 아니게 상한을 둔다.
    SYNC_TOTAL_TIMEOUT_SEC = 40.0
    # 같은 딜러 좌석이 이만큼(로컬 픽셀 판별 + AI 응답을 합쳐) 나오면 "일관되게
    # 유지됨"으로 보고 동기화를 성공 처리한다.
    SYNC_CONSISTENCY_VOTES = 2
    SYNC_MIN_OCCUPIED = 4
    SYNC_MAX_OCCUPIED = 6

    def _try_local_dealer_detect(self, img) -> int | None:
        seat_crops = {
            seat.index: self._safe_crop(img, self._scaled(self._dealer_zone(seat.region)))
            for seat in self.layout.seats
        }
        return dealer_position.find_dealer_seat_by_pixel(seat_crops, self.layout.dealer_marker_colors)

    # task.md "새 핸드 감지 로직 보완": 결과가 찍힌 뒤 이만큼 프레임을 기다렸다가
    # (화면이 다음 핸드로 넘어갈 시간을 줌) 딜러 위치를 확인하기 시작한다.
    NEW_HAND_DEALER_CHECK_DELAY_FRAMES = 10
    # 그 뒤로는 매 프레임 전체 좌석을 스캔하지 않고 이 주기로만 확인한다(비용 절약).
    NEW_HAND_DEALER_RECHECK_EVERY_FRAMES = 5
    # 딜러 탐지가 카드 뒷면과 헷갈릴 수 있어서(기존에 문서화된 한계), 같은 새
    # 딜러 좌석이 이만큼 연속으로 나와야 확정한다(오탐 방지).
    NEW_HAND_DEALER_CONFIRM_VOTES = 2
    # 로컬 픽셀 판별이 이만큼(프레임) 계속 못 잡으면 AI로 한 번 보조 확인한다
    # (실측 확인: 핸드 사이 구간엔 카드 뒷면이 자주 겹쳐서 로컬 판별이 계속
    # "여러 좌석 동시 매치"로 실패하는 경우가 많았다 - task.md가 예시로 든
    # "(필요 시) AI를 보조로 사용"에 해당).
    NEW_HAND_DEALER_AI_FALLBACK_FRAMES = 30

    def _check_dealer_moved_new_hand(self, img) -> None:
        """보드가 0장에서 전혀 안 바뀌고 끝나는 핸드(프리플랍 폴드아웃) 다음 핸드를
        잡기 위한 보조 경로. 이번 핸드가 "끝났다고 볼 수 있는" 신호(결과가 찍혔거나,
        히어로가 이미 폴드해서 더 볼 게 없는 상태)가 있는데도 보드가 여전히 0장이면,
        딜러 버튼이 실제로 이동했는지 직접 확인해서 새 핸드를 시작한다.

        (실전 영상으로 재확인하다가 발견: 히어로가 폴드한 핸드는 결과 자체를 안
        찍으므로(task.md "히어로 폴드 후 기록 중단" A안) _hand_result_seen이 영영
        True가 안 돼서, 히어로 폴드 + 보드 0장 유지가 겹치면 이 경로가 아예 안
        돌고 있었다 - 그래서 히어로 폴드도 "핸드 끝남" 신호로 같이 본다.)"""
        if not (self._hand_result_seen or self._hero_folded_this_hand):
            return
        with self._state_lock:
            street, count = self._street, self._board_count
        if street != DEFAULT_STREET or count != 0:
            return  # 이미 board_count 변화로 처리됐거나(플랍까지 간 핸드), 아직 핸드 진행 중

        self._frames_since_result += 1
        if self._frames_since_result < self.NEW_HAND_DEALER_CHECK_DELAY_FRAMES:
            return
        if self._frames_since_result % self.NEW_HAND_DEALER_RECHECK_EVERY_FRAMES != 0:
            return

        new_dealer = self._try_local_dealer_detect(img)
        if new_dealer is None or new_dealer == self._dealer_seat:
            self._pending_new_dealer = None
            self._pending_new_dealer_streak = 0
            # 로컬 판별이 오래 실패하면(카드 뒷면과 겹쳐 계속 애매함) AI로 한 번
            # 보조 확인한다 - 대기 사이클당 한 번만(비용 억제).
            if self._frames_since_result >= self.NEW_HAND_DEALER_AI_FALLBACK_FRAMES and not self._ai_new_hand_check_done:
                self._ai_new_hand_check_done = True
                result = ai_seat_detector.detect_seats_via_ai(
                    img, seat_count=len(self.layout.seats), reason="new_hand_check"
                )
                if result is not None and result.dealer_seat is not None and result.dealer_seat != self._dealer_seat:
                    self._begin_new_hand(confirmed_dealer_seat=result.dealer_seat)
            return

        if self._pending_new_dealer == new_dealer:
            self._pending_new_dealer_streak += 1
        else:
            self._pending_new_dealer = new_dealer
            self._pending_new_dealer_streak = 1

        if self._pending_new_dealer_streak < self.NEW_HAND_DEALER_CONFIRM_VOTES:
            return  # 아직 확정 안 됨(오탐 방지) - 다음 확인 때 다시 본다

        self._begin_new_hand(confirmed_dealer_seat=new_dealer)

    def _run_sync_phase(self) -> None:
        start_time = time.time()
        dealer_votes: list[int] = []
        hero_seat_from_ai: int | None = None
        occupied_count: int | None = None

        for _attempt in range(self.SYNC_MAX_AI_CALLS):
            if self._stop_flag.is_set() or time.time() - start_time > self.SYNC_TOTAL_TIMEOUT_SEC:
                break
            img = window_capture.capture_window(self.hwnd)
            if img is None:
                time.sleep(self.interval_sec)
                continue
            self._capture_size = img.size

            # 로컬 딜러 픽셀 판별은 비용이 거의 없으니 매번 같이 시도해서 투표에 보탠다.
            local_dealer = self._try_local_dealer_detect(img)
            if local_dealer is not None:
                dealer_votes.append(local_dealer)

            result = ai_seat_detector.detect_seats_via_ai(img, seat_count=len(self.layout.seats), reason="sync")
            if result is not None:
                if result.dealer_seat is not None:
                    dealer_votes.append(result.dealer_seat)
                if result.hero_seat is not None:
                    hero_seat_from_ai = result.hero_seat
                if result.occupied_seats:
                    occupied_count = len(result.occupied_seats)

            if dealer_votes:
                best, count = Counter(dealer_votes).most_common(1)[0]
                if count >= self.SYNC_CONSISTENCY_VOTES:
                    break  # 이미 충분히 일관됨 - 더 기다리지 않고 바로 마무리한다

        if self._stop_flag.is_set():
            return  # 동기화 중 정지됨 - 상태 메시지도 남길 필요 없음(_run에서 "중지됨" 처리)

        best, count = (Counter(dealer_votes).most_common(1)[0] if dealer_votes else (None, 0))
        occupied_count = occupied_count or len(self.layout.seats)
        success = best is not None and count >= self.SYNC_CONSISTENCY_VOTES and \
            self.SYNC_MIN_OCCUPIED <= occupied_count <= self.SYNC_MAX_OCCUPIED

        if success:
            self._dealer_seat = best
            self._dealer_recheck_needed = False
            if hero_seat_from_ai is not None:
                self._hero_seat_override = hero_seat_from_ai
            # task.md "새 핸드 감지 로직 보완" 5절: 이후 [N게임 시작] (M인 테이블)도
            # 이 값을 그대로 써서 동기화 때 얻은 인원수와 표기가 섞이지 않게 한다.
            self._active_seat_count = occupied_count
            self._synced_successfully = True
            self._emit_status("동기화 완료. 다음 핸드부터 기록 시작")
            self._emit_line(
                f"[동기화 완료] 딜러: {self._position_for(best)} / "
                f"나 위치: {self._position_for(self._hero_seat_index())} / 인원: {occupied_count}인",
                "hand_header",
            )
        else:
            self._emit_status("동기화 실패 - 로컬 모드로 진행합니다")

    def _scaled(self, region: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        return resolve_ratio_region(region, self._capture_size)

    @staticmethod
    def _safe_crop(img, region):
        try:
            return img.crop(region)
        except Exception:
            return None

    def _ocr_lines_safe(self, crop) -> list:
        if crop is None:
            return []
        try:
            return ocr_engine.recognize_lines(crop, lang=self.ocr_lang)
        except Exception:
            return []

    def _ocr_many_parallel(self, img, jobs: list[tuple]) -> dict:
        """jobs: [(key, region), ...]. 각 영역을 잘라서 스레드풀로 동시에 OCR한다.
        (crop 자체는 스레드 밖에서 미리 해둔다 - 같은 이미지를 여러 스레드가 동시에
        crop해도 안전할 거란 보장이 없어서, 실측 검증한 "미리 crop 후 OCR만 병렬"
        방식을 그대로 따른다.) {key: [OcrLine, ...]} 를 반환한다."""
        crops = {key: self._safe_crop(img, region) for key, region in jobs}
        futures = {key: self._executor.submit(self._ocr_lines_safe, crop) for key, crop in crops.items()}
        return {key: fut.result() for key, fut in futures.items()}

    def _process_frame(self, img) -> None:
        self._capture_size = img.size
        self._frame_count += 1

        # 1) 보드 영역: OCR 대신 카드 장수만 픽셀 밝기로 판별해서 스트리트를 정한다.
        board_region = self._scaled(self.layout.board_region)
        try:
            count = board_reader.detect_card_count(img, board_region)
        except Exception:
            count = self._board_count
        self._update_street(count)

        # 0-0-1) task.md "새 핸드 감지 로직 보완": 보드가 0장에서 안 바뀌고 끝나는
        #    핸드(프리플랍 폴드아웃) 다음 핸드는 위 _update_street만으로는 못
        #    잡는다 - 결과가 이미 찍힌 뒤 딜러 버튼이 실제로 옮겨갔는지 직접
        #    확인해서 보조로 잡는다.
        self._check_dealer_moved_new_hand(img)

        # 0-0) task.md "동기화 단계 도입" 4절: 동기화 직후엔 "지금 진행 중인 핸드"를
        #    무시하고, 실제로 새 핸드가 감지될 때(_update_street가 방금 새 핸드로
        #    판단했을 때)부터 본 기록을 시작한다. 그 전까지는 보드 장수 판별(위
        #    _update_street) 말고는 아무 것도 안 한다 - 지저분한 중간 로그 방지.
        if not self._recording_active:
            if self._just_entered_new_hand:
                self._recording_active = True
                self._emit_status("기록 중")
            else:
                return

        # 0-1) task.md "AI 하이브리드 도입": 히어로가 이번 핸드에 이미 폴드했으면
        #    다음 핸드 시작(위 _update_street가 보드 0장을 감지해서 리셋하는 순간)
        #    까지 더 볼 게 없다 - AI 호출은 물론 아우라/밝기/OCR도 다 건너뛴다.
        if self._hero_folded_this_hand:
            return

        # 0-2) task.md "동기화 단계 도입" 6절: 동기화가 이미 성공했으면 딜러/히어로는
        #    "고정된 값"을 계속 쓰고 매 핸드 AI를 다시 부르지 않는다(동기화 실패
        #    시에는 예전처럼 핸드 시작 때 한 번 더 시도해본다 - 그마저도 실패하면
        #    _hero_seat_index()의 로컬 기본값 폴백으로 계속 진행된다).
        if not self._synced_successfully:
            self._maybe_call_ai_for_hand(img)

        # 1-1) task.md/task2.md 1순위: 파란 아우라로 "지금 누구 턴인지"부터 잡는다.
        #    규칙 기반 예측(_turn_seat_index)이 어긋났어도 이 실제 시각 신호가
        #    다시 맞춰준다. OCR이 아니라 픽셀 색 비율만 보므로 매 프레임 다 봐도 싸다.
        self._check_turn_aura(img)

        # 1-2) "겹"(Fold) OCR은 원리적으로 안 되니(위 _check_fold_by_brightness 주석
        #    참고), 좌석별 밝기 급락으로 Fold를 판정한다. 보드 밝기 판별처럼 OCR이
        #    아니라 픽셀 평균만 보므로 매 프레임 다 확인해도 비용이 작다.
        self._check_fold_by_brightness(img)

        # 2) 좌석별 "행동 텍스트 영역"(좌석 박스 상단부)만 병렬로 OCR한다(전체 화면
        #    OCR 아님, 각각 좁은 고정 영역). 딜러 위치를 알면 턴 좌석 + 순환 커버리지
        #    몇 곳만 보고, 모르면 안전하게 5곳 다 본다(_seats_to_watch 참고). 결과 패널은
        #    여기 안 끼워 넣는다 - 줄 수가 많아서 그것만 180~320ms가 걸려, 같이 넣으면
        #    빠른 좌석 호출까지 그 느린 호출을 기다리느라 배치 전체가 늘어지는 걸
        #    실측으로 확인했다. 결과 패널은 아래 5)에서 따로, 드물게 확인한다.
        watched_seats = self._seats_to_watch(img)
        jobs = [(seat.index, self._scaled(self._action_zone(seat.region))) for seat in watched_seats]
        seat_lines = self._ocr_many_parallel(img, jobs)

        # 2-2) 베팅 금액 재시도(task.md "베팅 금액 보완" - 위 _check_pending_amounts
        #    주석 참고). 방금 등록된 게 있어도 상관없다 - 이번 프레임엔 아직 못
        #    찾을 뿐, 다음 프레임부터 자연히 재시도된다.
        self._check_pending_amounts(img)

        # 3) 딜러 버튼 위치는 매 프레임 다시 찾지 않는다. 재확인이 필요하거나(새 핸드
        #    시작), 일정 프레임마다 한 번씩만 찾는다. 이때도 먼저 "이럴 것"이라고 예측한
        #    좌석 한 곳만 확인해보고(새 핸드면 이전 딜러의 다음 자리, 주기적 점검이면
        #    지금 딜러 자리 그대로인지), 거기서 못 찾았을 때만 5곳 전체를 병렬로 훑는
        #    예비 로직으로 넘어간다.
        #    (버그 수정: 예전엔 "아직 못 찾았으면" 조건이 단독으로도 매번 전체 스캔을
        #    트리거해서, 딜러 표시가 화면에 없는 동안 매 프레임 5곳 전체 OCR을 반복
        #    하느라 계속 느려지는 문제가 있었다. 못 찾은 상태도 이제 똑같이 주기적으로만
        #    재시도한다 - 다만 딜러를 아예 한 번도 못 찾았을 때는 더 자주(짧은 주기로)
        #    재시도해서 초반에 너무 오래 걸리지 않게 한다.)
        periodic_check = self._frame_count % self.DEALER_RECHECK_EVERY_FRAMES == 0
        never_found_retry = (
            self._dealer_seat is None and self._frame_count % self.DEALER_INITIAL_SEARCH_RETRY_FRAMES == 0
        )
        if self._dealer_recheck_needed or periodic_check or never_found_retry:
            dealer = None
            quick_candidate = None
            if self._dealer_recheck_needed and self._dealer_candidate is not None:
                quick_candidate = self._dealer_candidate  # 새 핸드 -> 이전 딜러 다음 자리부터
            elif not self._dealer_recheck_needed and self._dealer_seat is not None:
                quick_candidate = self._dealer_seat  # 주기적 안전점검 -> 지금 자리 그대로인지

            # 딜러 표시는 OCR이 아니라 픽셀(원 검출)로 판별한다 - "D" 같은 고립된
            # 글자 하나는 Windows OCR이 원리적으로 못 읽는 것을 실측(합성 테스트)으로
            # 확인했다. dealer_position.find_dealer_seat_by_pixel 참고. 원 내부 색은
            # table_layout.json의 dealer_marker_colors로 게임마다 다르게 설정한다
            # (ClubWPT GOLD=흰색, 코인포커=빨간색). OCR 기반(한게임 스타일 "DEALER"
            # 전체 단어)은 폴백으로만 남겨둔다.
            dealer_colors = self.layout.dealer_marker_colors
            if quick_candidate is not None:
                seat = self.layout.seat_by_index(quick_candidate)
                if seat is not None:
                    crop = self._safe_crop(img, self._scaled(self._dealer_zone(seat.region)))
                    dealer = dealer_position.find_dealer_seat_by_pixel({quick_candidate: crop}, dealer_colors)

            if dealer is None:
                seat_crops = {
                    seat.index: self._safe_crop(img, self._scaled(self._dealer_zone(seat.region)))
                    for seat in self.layout.seats
                }
                dealer = dealer_position.find_dealer_seat_by_pixel(seat_crops, dealer_colors)

            if dealer is None:
                dealer_jobs = [(seat.index, self._scaled(self._dealer_zone(seat.region))) for seat in self.layout.seats]
                dealer_lines = self._ocr_many_parallel(img, dealer_jobs)
                dealer = dealer_position.find_dealer_seat(dealer_lines)

            self._dealer_recheck_needed = False
            if dealer is not None:
                self._dealer_seat = dealer
                if self._turn_seat_index is None:
                    with self._state_lock:
                        is_preflop = self._street == DEFAULT_STREET
                    offset = self.PREFLOP_START_OFFSET if is_preflop else self.POSTFLOP_START_OFFSET
                    self._turn_seat_index = self._seat_at_offset(dealer, offset)
            elif self._dealer_seat is None:
                self._emit_status(f"딜러 버튼을 찾는 중... (총 {self.event_count}건 기록됨)")

        # 4) 이번 프레임에 실제로 본 좌석들만, 새로 나타난 텍스트를 행동으로 판별해서
        #    기록한다(중복 방지). 딜러를 아직 못 찾았어도 행동 기록 자체는 멈추지
        #    않는다(포지션은 "?").
        for seat in watched_seats:
            new_lines = self._seat_diffs[seat.index].update(seat_lines.get(seat.index, []))
            for line in new_lines:
                self._handle_seat_line(seat.index, line, img)

        # 5) 우측 "내기록" 패널: 핸드가 끝나면 "닉네임 : +52만 골드"처럼 승패가 찍힌다.
        #    (반드시 "내기록" 탭이 선택돼 있어야 보인다 - "채팅" 탭이면 이 영역엔 아무
        #    결과도 안 나온다.) +로 시작하는 줄만 승리로 보고 기록한다. 승리는 핸드당
        #    한 번뿐이라 매 프레임 볼 필요가 없어서, 일정 프레임마다만 확인한다(느린
        #    호출이라 행동 감지 핫패스에서 분리했다 - 위 2)의 주석 참고).
        if self.layout.result_panel_region and self._frame_count % self.RESULT_PANEL_CHECK_EVERY_FRAMES == 0:
            region = self._scaled(self.layout.result_panel_region)
            result_lines = self._ocr_lines_safe(self._safe_crop(img, region))
            for line in self._result_diff.update(result_lines):
                self._handle_result_line(line)

        # 5-2) 코인포커: 별도 결과 패널이 없고, 이긴 좌석의 홀카드 위에 "+금액"이
        #    잠깐 뜬다(실측 확인) - table_layout.json에 result_zone_top_extend가
        #    설정된 게임에서만 동작한다(0이면 그냥 건너뜀, ClubWPT 등 기존 동작 그대로).
        #    이것도 핸드당 한 번뿐이라 결과 패널과 같은 주기로, 드물게만 확인한다.
        if self.layout.result_zone_top_extend > 0 and self._frame_count % self.RESULT_PANEL_CHECK_EVERY_FRAMES == 0:
            self._check_per_seat_win_amounts(img)

    # 스트리트 전환을 실제로 확정하기까지 같은 값이 연속으로 몇 프레임 나와야 하는지.
    # board_reader의 픽셀 판별이 한 프레임만 순간적으로 오검출해도(예: 5장인데
    # 어쩌다 3장으로 잡힘), 예전엔 그걸 그대로 "새 핸드 시작"으로 취급해서 스트리트
    # 헤더가 뒤섞이고, 그 순간 중복방지 기록(_last_action_by_seat)까지 같이
    # 지워져서 같은 행동이 다시 찍히는 문제가 있었다(실측 확인, 두 증상이 사실
    # 하나의 원인이었다) - 이 확정 절차로 그 순간적인 오검출을 걸러낸다.
    STREET_CONFIRM_FRAMES = 2

    def _update_street(self, count: int) -> None:
        self._just_entered_new_hand = False  # 이번 호출에서 새로 판단(아래에서 True로 바뀔 수 있음)
        with self._state_lock:
            current_count = self._board_count

        if count == current_count:
            self._pending_board_count = None
            self._pending_board_count_streak = 0
            return

        # 프리플랍→플랍→턴→리버 순서만 허용한다. 0장(새 핸드)이 아니면서 지금보다
        # 장수가 줄어드는 건(예: 리버 5장 상태에서 갑자기 3장) 실제로 있을 수 없는
        # 상태라 - 오검출로 보고 아예 대기열에 올리지도 않는다.
        if count != 0 and count < current_count:
            self._pending_board_count = None
            self._pending_board_count_streak = 0
            return

        # 같은 값이 연속으로 나와야 확정한다 (한 프레임짜리 순간적 오검출 방지).
        if self._pending_board_count == count:
            self._pending_board_count_streak += 1
        else:
            self._pending_board_count = count
            self._pending_board_count_streak = 1

        if self._pending_board_count_streak < self.STREET_CONFIRM_FRAMES:
            return  # 아직 확정 안 됨 - 기존 스트리트/기록 상태를 그대로 유지한다.

        self._pending_board_count = None
        self._pending_board_count_streak = 0

        street = board_reader.street_for_count(count)

        if street == DEFAULT_STREET:
            # 보드가 0장으로 돌아왔다 -> 새 핸드가 시작된 것으로 본다(수동 "다음 게임"
            # 버튼은 실제로는 잘 안 눌려서, 이 신호가 실질적인 새 핸드 감지 수단이다).
            # (플랍을 한 번도 못 보고 끝나는 핸드에서는 이 경로 자체가 안 도는데,
            # 그건 _check_dealer_moved_new_hand가 딜러 이동으로 따로 잡는다.)
            self._begin_new_hand()
            return

        with self._state_lock:
            self._street = street
            self._board_count = count
        # 스트리트가 바뀌면 새 턴이 시작된 것이므로 "나 : (대기)" 중복 방지 플래그도
        # 다시 연다(task.md "히어로 (대기) 표시 안정화").
        self._hero_wait_announced_this_turn = False

        # 포스트플랍(플랍/턴/리버)은 SB(딜러+1)부터 시작. 딜러를 아직 모르면 그냥
        # None으로 둬서 다음 프레임엔 안전하게 5곳 전부 보게 한다(_seats_to_watch).
        if self._dealer_seat is not None:
            self._turn_seat_index = self._seat_at_offset(self._dealer_seat, self.POSTFLOP_START_OFFSET)
        else:
            self._turn_seat_index = None
        # 핸드 내내 딜러 버튼은 고정이고 스트리트가 바뀐다고 자리가 바뀌지 않으므로,
        # 여기서는 딜러 재확인을 걸지 않는다. (예전엔 걸었었는데, 턴/리버 전환마다
        # 느린 전체 좌석 OCR이 같이 실행돼서 그 타이밍에 행동을 놓치는 원인이었다.)

        # task.md 출력 형식: 포스트플랍(플랍/턴/리버)만 "보드: N장"을 붙인다.
        self._emit_line(f"[{street}] 보드: {count}장", "street_header")

    def _begin_new_hand(self, confirmed_dealer_seat: int | None = None) -> None:
        """새 핸드 시작 처리를 한 곳에 모은다. 두 경로에서 호출된다:
        1) _update_street: 보드가 정상적으로 0장으로 돌아온 일반적인 경우
        2) _check_dealer_moved_new_hand: 보드가 0장에서 한 번도 안 바뀌고 끝난
           핸드(프리플랍 폴드아웃) 다음 핸드 - 딜러 버튼이 실제로 옮겨간 걸 보고 잡는다
           (task.md "새 핸드 감지 로직 보완", confirmed_dealer_seat로 새 딜러를 바로 확정한다).
        """
        if confirmed_dealer_seat is not None:
            self._dealer_seat = confirmed_dealer_seat
            self._dealer_recheck_needed = False
            self._dealer_candidate = None
        else:
            # 딜러 버튼은 새 핸드마다 한 자리씩만 옮겨가므로, 이전 딜러의 다음
            # 자리부터 먼저 확인해보고(다음 _process_frame에서), 못 찾을 때만
            # 전체를 훑는다.
            self._dealer_recheck_needed = True
            self._dealer_candidate = (
                self._next_in_rotation(self._dealer_seat) if self._dealer_seat is not None else None
            )

        with self._state_lock:
            self._street = DEFAULT_STREET
            self._board_count = 0
            self._hand_no += 1
            hand_no = self._hand_no

        if self._dealer_seat is not None:
            self._turn_seat_index = self._seat_at_offset(self._dealer_seat, self.PREFLOP_START_OFFSET)
        else:
            self._turn_seat_index = None

        # task.md "새 핸드 감지 로직 보완" 5절: 동기화 때 얻은 인원수와 여기 표기를
        # 같은 기준으로 통일한다(있으면 그 값, 없으면 기존처럼 설정 좌석 수).
        seat_count = self._active_seat_count or len(self.layout.seats)
        self._emit_line(f"[{hand_no}게임 시작] ({seat_count}인 테이블)", "hand_header")
        self._emit_line(f"[{DEFAULT_STREET}]", "street_header")

        self._just_entered_new_hand = True  # task.md "동기화 단계 도입" - _process_frame이 이걸 보고 기록을 켠다
        self._hero_wait_announced_this_turn = False
        self._last_action_by_seat.clear()
        # 밝기 기반 Fold 판정(task.md "Fold 밝기 감지")도 핸드마다 새로 본다 -
        # 기준 밝기(baseline)는 그대로 들고 있어도 되지만("이번 핸드에 폴드한
        # 좌석" 집합만 지우면 됨), 지난 핸드에 어두워진 채로 다음 핸드가 시작되는
        # 경우도 있어서 기준 밝기도 같이 지워 이번 핸드 첫 프레임 값으로 다시 잡는다.
        self._folded_seats_this_hand.clear()
        self._seat_brightness_baseline.clear()
        self._seat_dark_streak.clear()
        self._pending_amount.clear()  # 지난 핸드에 못 채운 금액은 이제 의미 없음
        # 아우라 히스토리/현재 배우도 새 핸드 기준으로 새로 본다(기준 밝기 baseline
        # 자체는 조명/아바타 그림처럼 핸드가 바뀌어도 그대로라 안 지운다).
        self._seat_aura_recent_frames.clear()
        self._current_actor = None
        # 새 핸드가 시작됐으니 AI 호출권/히어로 폴드 상태/이전 핸드 홀카드도 새로 본다.
        # (히어로 좌석 override는 여기서 지우지 않는다 - task.md "동기화 단계
        # 도입" 6절 "히어로 좌석은 고정된 값을 계속 사용한다": 동기화나 예전
        # AI 호출로 한 번 정해지면 세션 내내 그대로 쓴다.)
        self._ai_call_done_this_hand = False
        self._hero_cards = ""
        self._hand_start_frame_count = 0
        self._hero_folded_this_hand = False
        # task.md "새 핸드 감지 로직 보완": 결과가 이미 찍혔는데 보드가 0장에서
        # 안 바뀌는 상태를 감지하기 위한 상태도 새 핸드 기준으로 리셋한다.
        self._hand_result_seen = False
        self._frames_since_result = 0
        self._pending_new_dealer = None
        self._pending_new_dealer_streak = 0
        self._ai_new_hand_check_done = False
        self._pending_board_count = None
        self._pending_board_count_streak = 0

    # task.md "AI 하이브리드 도입" 2절: 새 핸드 시작 후 홀카드가 다 배분될 시간을
    # 잠깐 준 뒤(고정 지연 프레임 수 - 실제 "카드가 보이는지" 픽셀 판별은 관전
    # 모드라 실측을 못 해서, 대신 단순하고 검증하기 쉬운 고정 지연으로 근사했다)
    # 딱 한 번만 AI를 부른다. 성공하든 실패하든 이 핸드엔 다시 안 부른다(1절
    # "AI는 매 프레임 호출하지 않는다, 핸드당 1회만" 원칙).
    # task.md(위치 인식 실패 수정): 5프레임(약 1초, 실제 0.2초 주기 기준)은 카드
    # 배분 애니메이션이 덜 끝났을 수 있어 10프레임(약 2초)으로 늘렸다.
    AI_CALL_DELAY_FRAMES = 10

    def _maybe_call_ai_for_hand(self, img) -> None:
        if self._ai_call_done_this_hand or self._hero_folded_this_hand:
            return
        with self._state_lock:
            street = self._street
        if street != DEFAULT_STREET:
            return  # 이미 프리플랍이 지났으면 이번 핸드는 늦었다 - 다음 핸드에 다시 시도
        self._hand_start_frame_count += 1
        if self._hand_start_frame_count < self.AI_CALL_DELAY_FRAMES:
            return
        self._ai_call_done_this_hand = True  # 성공/실패 상관없이 이번 핸드는 여기서 끝

        result = ai_seat_detector.detect_seats_via_ai(img, seat_count=len(self.layout.seats))
        ai_succeeded = result is not None and result.dealer_seat is not None
        if result is not None:
            if result.dealer_seat is not None:
                self._dealer_seat = result.dealer_seat
                self._dealer_recheck_needed = False  # AI가 확정해줬으니 이번 핸드는 다시 안 찾는다
            # task.md "관전모드에서도 히어로 자리는 항상 하단 중앙" 폴백: AI가
            # hero_seat를 못 줘도(null) 좌석 설정값(하단중앙)을 그대로 쓴다 -
            # _hero_seat_index()가 override 없으면 self.layout.hero_seat_index로
            # 알아서 떨어지므로, 여기서는 AI가 준 값이 있을 때만 override한다.
            if result.hero_seat is not None:
                self._hero_seat_override = result.hero_seat
            if result.hero_cards:
                self._hero_cards = result.hero_cards

        # task.md "로그 출력 규칙 정리": AI가 실패했거나(키 없음/타임아웃/파싱 실패)
        # 딜러 좌석을 못 줬으면, "딜러: ?" 같은 줄을 반복해서 찍지 않는다 - 짧은
        # 실패 안내만 한 줄 남기고 조용히 로컬 모드로 넘어간다.
        if not ai_succeeded and self._dealer_seat is None:
            self._emit_line("딜러/위치 인식 실패 - 로컬 모드로 진행", "hand_header")
            return

        self._emit_hand_info_block()

    def _emit_hand_info_block(self) -> None:
        """task.md "핸드 시작 시 기록 형식": 딜러 좌석을 실제로 알 때만(AI든 로컬
        판별이든) 딜러/내 위치/내 홀카드 3줄을 찍는다. "?"를 반복 출력하지 않는다."""
        if self._dealer_seat is None:
            return
        dealer_label = self._position_for(self._dealer_seat)
        hero_idx = self._hero_seat_index()
        hero_label = self._position_for(hero_idx) if hero_idx is not None else "?"
        cards_text = self._hero_cards if self._hero_cards else "(관전모드)"
        self._emit_line(f"딜러: {dealer_label}", "hand_header")
        self._emit_line(f"나 위치: {hero_label}", "hand_header")
        self._emit_line(f"내 홀카드: {cards_text}", "hand_header")

    def _hero_seat_index(self) -> int | None:
        """task.md "AI 하이브리드": AI가 이번 핸드에 히어로 좌석을 알려줬으면
        그걸 우선 쓰고, 없으면 table_layout.json의 기본값을 쓴다."""
        if self._hero_seat_override is not None:
            return self._hero_seat_override
        return self.layout.hero_seat_index

    def _position_for(self, seat_index: int) -> str:
        if self._dealer_seat is None:
            return "?"  # 딜러를 아직 못 찾았어도 행동 기록은 계속하고, 포지션만 임시로 "?"
        offset_label = dealer_position.position_label(seat_index, self._dealer_seat, self.layout.clockwise_order)
        return self._named_position(offset_label)

    def _named_position(self, offset_label: str) -> str:
        """dealer_position.position_label()의 "딜러"/"딜러+N"을 표준 포지션 이름
        (UTG/HJ/CO/BTN/SB/BB 등)으로 바꾼다. 표에 없는 인원수면 원래 라벨 그대로."""
        if offset_label == "딜러":
            offset = 0
        elif offset_label.startswith("딜러+"):
            try:
                offset = int(offset_label[len("딜러+"):])
            except ValueError:
                return offset_label
        else:
            return offset_label  # "?" 등
        names = POSITION_NAMES_BY_TABLE_SIZE.get(len(self.layout.clockwise_order))
        if names and 0 <= offset < len(names):
            return names[offset]
        return offset_label

    def _handle_seat_line(self, seat_index: int, line, img=None) -> None:
        parsed = action_parser.parse_line(line.text)
        if parsed.kind != "action" or parsed.action_type not in LOGGED_ACTION_TYPES:
            return  # 행동으로 분류되지 않은 텍스트(닉네임/칩 수량 등)는 기록하지 않는다.

        # 행동 배지 자체에는 보통 금액이 없다(좌석 밖 칩 옆에 따로 뜬다 - _bet_zone
        # 참고). Fold/Check처럼 원래 금액이 없는 행동은 건드리지 않는다.
        if not parsed.amount and parsed.action_type not in action_parser.ACTIONS_WITHOUT_AMOUNT and img is not None:
            parsed.amount = self._read_bet_zone_amount(seat_index, img)

        with self._state_lock:
            hand_no = self._hand_no
            street = self._street

        # OCR 잡음 때문에 같은 행동 배지가 프레임마다 살짝 다른 텍스트("1000골드베트 0"
        # vs "1000골드 베트 베")로 잡혀서 원문 기준 중복 방지(text_diff)를 통과해버리는
        # 경우가 실측에서 나왔다. 그래서 "이 좌석이 이 핸드/스트리트에 마지막으로 기록한
        # (행동, 금액)"과 같으면 다시 기록하지 않는다(의미 기준 중복 방지). hand_no까지
        # 키에 넣어서, 새 핸드로 넘어갔는데 어떤 이유로든 _last_action_by_seat가 안
        # 지워진 경우에도 이전 핸드 기록과 안 섞이게 이중으로 막는다.
        signature = (hand_no, street, parsed.action_type, parsed.amount)
        if self._last_action_by_seat.get(seat_index) == signature:
            return
        self._last_action_by_seat[seat_index] = signature

        # 행동이 감지된 좌석 기준으로 턴을 다음 자리로 넘긴다. 예측이 틀렸어도(폴드로
        # 순서가 건너뛰었거나 등) 실제로 행동한 좌석을 기준으로 다시 맞추므로 자연히
        # 보정된다 - 예측을 그대로 밀어붙이지 않는다.
        self._turn_seat_index = self._next_in_rotation(seat_index)

        position = self._position_for(seat_index)
        is_hero = self._hero_seat_index() == seat_index

        # task.md "AI 하이브리드 도입" 4절: 히어로가 폴드하면 이 핸드는 더 이상
        # 상세 분석을 안 한다(다음 핸드 시작 감시만) - 아래 _process_frame에서 이
        # 플래그를 보고 대부분의 프레임 작업을 건너뛴다.
        if is_hero and parsed.action_type == "Fold":
            self._hero_folded_this_hand = True
        if is_hero:
            # 실제로 행동했으니 이번 턴의 "(대기)" 몫은 끝 - 다음에 다시 내 턴이
            # 오면(같은 스트리트에서라도) 새로 "(대기)"를 찍을 수 있게 연다.
            self._hero_wait_announced_this_turn = False

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp, hand_no, street, position, parsed.action_type,
            parsed.amount, "예" if is_hero else "", parsed.raw_text,
        ]
        ok = self.xlsx_logger.append(row, highlight=is_hero)
        self.event_count += 1
        if not ok:
            self._emit_status(self.xlsx_logger.last_error or "저장 실패")

        # task.md "결과 형식 히어로 중심 개선" 3절: 다른 플레이어는 기존처럼
        # "포지션 ] 행동" 형태를 쓰고, 내 행동만 "나   : 행동" 형태로 따로 표시한다
        # (이전엔 "★ 나"로 자리 이름만 바꿨는데, 이번엔 구분자 자체를 다르게 둔다).
        # 엑셀의 "포지션" 열은 그대로 실제 포지션 이름을 남긴다("내 행동" 열이 이미
        # "예"로 표시되므로 정보가 겹치지 않는다).
        if is_hero:
            display_line = _format_hero_line(parsed.action_type, parsed.amount)
        else:
            display_line = _format_action_line(position, parsed.action_type, parsed.amount)
        self._emit_line(display_line, "hero_action" if is_hero else "action")

        # task.md "베팅 금액 보완": 방금 못 읽었으면(위에서 한 번 시도했지만 실패)
        # 포기하지 않고, 칩 숫자가 화면에 남아있는 동안 몇 프레임 더 다시 읽어본다
        # (_check_pending_amounts 참고). 잘못된 값을 억지로 붙이지 않기 위해, 실제로
        # 다시 읽어서 "확실히" 찾았을 때만 채운다 - 못 찾으면 그냥 비워둔 채로 만료된다.
        if (
            not parsed.amount
            and parsed.action_type not in action_parser.ACTIONS_WITHOUT_AMOUNT
            and ok
            and self.xlsx_logger.last_row is not None
        ):
            self._pending_amount[seat_index] = {
                "hand_no": hand_no,
                "street": street,
                "row": self.xlsx_logger.last_row,
                "position": position,
                "action_type": parsed.action_type,
                "is_hero": is_hero,
                "frames_left": self.PENDING_AMOUNT_MAX_FRAMES,
            }

    def _handle_result_line(self, line) -> None:
        parsed = action_parser.parse_result_line(line.text)
        if parsed is None:
            return
        nickname, sign, amount = parsed
        if sign != "+":
            return  # 딜러비/손실(-)은 승리 표시가 아니므로 기록하지 않는다.

        with self._state_lock:
            hand_no = self._hand_no
            street = self._street

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [timestamp, hand_no, street, nickname, "Win", amount, "", line.text]
        ok = self.xlsx_logger.append(row, highlight=False)
        self.event_count += 1
        if not ok:
            self._emit_status(self.xlsx_logger.last_error or "저장 실패")

        # task.md 출력 형식: "[N게임 결과] 포지션 승 (+금액)". 결과 패널은 닉네임만
        # 주기 때문에(어느 좌석인지는 안 알려줌) 좌석/포지션으로 못 바꾸고 닉네임을
        # 그대로 쓴다 - 닉네임<->좌석 매칭은 이번 범위 밖(승/패 자체는 정확히 잡힘).
        self._emit_line(f"[{hand_no}게임 결과] {nickname} 승 (+{amount})", "result")
        # task.md "새 핸드 감지 로직 보완": 결과가 찍혔다 = 이번 핸드는 끝났다는
        # 신호. 보드가 0장에서 안 바뀌는 핸드였으면 이걸 보고 딜러 이동 감시를 시작한다.
        self._hand_result_seen = True

    def _check_per_seat_win_amounts(self, img) -> None:
        """코인포커: 좌석별로 확장한 영역(_result_zone)에서 "+금액"을 찾는다. 이
        방식은 어느 좌석에서 떴는지 이미 알기 때문에(패널 방식과 달리 닉네임 매칭이
        필요 없다) 그 좌석의 포지션(히어로면 "★ 나")을 바로 쓸 수 있다."""
        with self._state_lock:
            hand_no = self._hand_no
            street = self._street

        for seat in self.layout.seats:
            region = self._scaled(self._result_zone(seat.region))
            lines = self._ocr_lines_safe(self._safe_crop(img, region))
            new_lines = self._win_amount_diffs[seat.index].update(lines)
            for line in new_lines:
                amount = action_parser.parse_win_amount(line.text)
                if not amount:
                    continue
                # task2.md "승자 결정 신호(황금 아우라 + 금액)": "+금액" 텍스트만으로
                # 확정하지 않고, 같은 좌석에 실제로 골드 아우라가 같이 떠 있는지도
                # 본다(오탐 방지 - 실측 확인: 골드는 평소 거의 안 나타나는 색이라
                # 있으면 신뢰도가 높다).
                if not self._seat_has_gold_aura(seat.index, img):
                    continue
                # 승리 표시는 화면에 몇 초간 떠 있어서 여러 프레임에 걸쳐 다시 잡힐 수
                # 있다 - 이 좌석이 이번 핸드에 이미 보고한 적 있으면 다시 찍지 않는다.
                if self._reported_win_hand_by_seat.get(seat.index) == hand_no:
                    continue
                self._reported_win_hand_by_seat[seat.index] = hand_no

                is_hero = self._hero_seat_index() == seat.index
                position = HERO_DISPLAY_LABEL if is_hero else self._position_for(seat.index)

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                row = [timestamp, hand_no, street, position, "Win", amount, "예" if is_hero else "", line.text]
                ok = self.xlsx_logger.append(row, highlight=is_hero)
                self.event_count += 1
                if not ok:
                    self._emit_status(self.xlsx_logger.last_error or "저장 실패")

                self._emit_line(f"[{hand_no}게임 결과] {position} 승 (+{amount})", "result")
                # task.md "새 핸드 감지 로직 보완": 결과가 찍혔다 = 이번 핸드는
                # 끝났다는 신호(보드 0장에서 안 바뀌는 핸드 대응).
                self._hand_result_seen = True

    def _sleep_remaining(self, start_t: float) -> None:
        elapsed = time.time() - start_t
        remaining = self.interval_sec - elapsed
        if remaining > 0:
            self._stop_flag.wait(remaining)
