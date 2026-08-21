"""딜러 채팅 기록기를 메인 프로그램(main.py)에 연결하는 다리 (task.md "딜러 채팅
파서를 메인 파이프라인에 연결" / "히어로 매칭 확정 + 카드 인식(AI)" / "99%를
위한 안정화").

기존 Recorder(src/recorder.py, 아우라/밝기/딜러버튼 기반)는 그대로 두고 전혀
건드리지 않는다 - 완전히 새로운 기록 경로다. main.py가 기존에 Recorder에게
기대하던 것과 같은 인터페이스(start/stop/is_running, on_line/on_status 콜백,
XlsxLogger 연동)로 감싸서, main.py 쪽 변경을 최소화한다.

캡처 -> DealerChatRecorder.process_frame -> (로그창 출력 + 엑셀 저장) 을 반복하는
백그라운드 스레드. 테이블 창은 다음 용도로 쓴다(없어도 딜러 채팅 기록 자체는
동작 - task.md "관전 모드/실제 플레이 모두에서 동작해야 함"):
  - 히어로 닉네임 자동 인식 (하단 중앙 고정 좌석 - task.md 1절, 관전 모드든
    실제 플레이든 자리는 같다. 매칭 성공 여부와 무관하게 항상 시도한다).
  - 히어로 홀카드 인식 (AI 비전, 핸드당 2장이 인식되거나 히어로가 폴드하거나
    시도 횟수를 다 쓸 때까지 재시도 - task.md "히어로 홀카드 인식 복구").
  - 보드 카드 인식 (AI 비전, 플랍/턴/리버 전환마다 최대 1회. 로컬 픽셀 판별
    (board_reader)은 AI 프롬프트용 힌트/즉시 표시용 폴백으로 쓰고, 최종 "N장"은
    AI가 실제로 읽은 카드 개수를 그대로 쓴다 - task.md "로그 안정화 4종" 3절).

task.md "99%를 위한 안정화" 3절: AI 호출(홀카드/보드)은 전부 스레드풀에 맡기고
메인 폴링 루프는 기다리지 않는다 - 딜러 채팅 OCR/파싱/새핸드감지(글자 기록의
핵심)는 AI 응답 속도와 완전히 무관하게 계속 진행된다. AI 결과는 준비되는 대로
콜백에서 뒤이어 한 줄 더 찍힌다(카드 값은 늦게 채워져도 된다는 게 지시서 전제).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import board_reader, card_reader, ocr_engine, window_capture
from .dealer_chat_parser import load_layout
from .dealer_chat_recorder import DealerChatRecorder
from .table_layout import load_layout as load_table_layout
from .table_layout import resolve_ratio_region
from .xlsx_writer import XlsxLogger

# 스트리트 이름 -> 그 시점에 보드에 깔려 있어야 할 카드 장수(AI 프롬프트용 힌트).
# 프리플랍은 보드가 없으므로 여기 없음 - 보드 카드 인식 자체를 시도하지 않는다.
BOARD_STREET_COUNTS = {"플랍": 3, "턴": 4, "리버": 5}

# task.md "히어로 홀카드 인식 복구": 딜러 채팅에 "게임 시작"이 찍히는 시점과
# 실제 테이블 화면에 카드가 다 펼쳐지는 시점 사이에 딜링 애니메이션 등으로 인한
# 딜레이가 있을 수 있다(실측 확인: 참조 프레임을 그대로 크롭해서 AI에 넣으면
# 정확히 읽히므로 좌표 문제가 아니라 타이밍 문제). 그래서 핸드당 정확히 1번만
# 캡처하는 대신, 2장이 인식되거나 히어로가 폴드할 때까지 이 횟수만큼 매 폴링마다
# 재시도한다(응답 대기 중에는 중복 제출 안 함 - _hole_cards_pending).
MAX_HOLE_CARD_ATTEMPTS = 5

# task.md "스트리트 헤더 보정(보드 장수 신호)": 딜러 채팅에 행동 텍스트가 아예
# 없는 스트리트(예: 전원 체크로 넘어간 플랍)는 DealerChatRecorder가 그 헤더를
# 영영 못 찍는다(텍스트가 하나도 안 잡히므로). 로컬 보드 장수 증가를 독립적인
# 보조 신호로 써서 그런 헤더도 강제로 채운다 - 프리플랍(0장)은 recorder가 항상
# 직접 찍으므로 여기 없다. 순서 그대로 두면 "앞으로만 진행" 규칙이 자연히 지켜짐.
BOARD_COUNT_MILESTONES = [(3, "플랍"), (4, "턴"), (5, "리버")]

# task.md "올인 쇼다운 리버 누락 수정": 전원 올인 후에는 더 이상 행동 텍스트가
# 없어서 로컬 보드 장수 신호(위)에만 의존하는데, 쇼다운 -> 다음 핸드 전환이
# 폴링 주기(예: 0.2초)보다 빠르면 "4장 -> 0장"으로 바로 건너뛰어 5장(리버)을 한
# 번도 못 보고 놓칠 수 있다. 이 값들은 "몇 프레임"이 아니라 "실제로 몇 번/몇 초
# 더 볼지"가 목적이라 폴링 주기와 무관하게 고정으로 둔다 - 아래 재확인 로직은
# 평소엔(리버를 정상적으로 이미 봤거나, 보드가 3/4장이 아니면) 전혀 타지 않는
# 분기라 일반 핸드/새 핸드 시작 속도에는 영향이 없다.
BOARD_RECHECK_ATTEMPTS = 4
BOARD_RECHECK_INTERVAL_SEC = 0.05

# task.md "히어로 탈락 감지 및 기록 자동 중지": 관전 모드로 잠깐 안 보이는
# 경우와 구분하기 위해 한 핸드만 안 보인다고 바로 판단하지 않는다. 권장 범위
# (3~5핸드) 중 중간값 - 너무 작으면 정상 관전/일시적 인식 실패를 탈락으로
# 오판하고, 너무 크면 탈락 후 불필요한 AI 호출이 오래 지속된다.
HERO_MISSING_HAND_THRESHOLD = 4


class DealerChatBridge:
    def __init__(
        self,
        chat_hwnd: int,
        table_hwnd: int | None,
        xlsx_logger: XlsxLogger,
        interval_sec: float = 1.5,
        ocr_lang: str = "ko",
        hero_nickname: str = "",  # 설정값 - 비어있으면 좌석 OCR 자동 인식으로 폴백
        on_line=None,  # callback(text:str, tag:str)
        on_status=None,  # callback(status_text:str)
        on_hero_missing=None,  # callback() - task.md "히어로 탈락 감지 및 기록 자동 중지"
    ):
        self.chat_hwnd = chat_hwnd
        self.table_hwnd = table_hwnd
        self.xlsx_logger = xlsx_logger
        self.interval_sec = interval_sec
        self.ocr_lang = ocr_lang
        self.on_line = on_line
        self.on_status = on_status
        self.on_hero_missing = on_hero_missing

        self._recorder = DealerChatRecorder(layout=load_layout(), ocr_lang=ocr_lang, interval_sec=interval_sec)
        # task.md "히어로 닉네임 설정값 매칭": 설정값이 있으면 그 세션 내내
        # 고정으로 쓰고(매 폴링 좌석 OCR 재탐지 안 함), 비어있을 때만 기존처럼
        # 매 폴링 좌석 OCR로 찾는다(_run 참고). 매칭 규칙(완전일치/접두어/편집거리1)
        # 자체는 dealer_chat_parser.names_match_prefix가 그대로 처리하므로 여기서
        # 새로 만들 것 없음 - 히어로 이름값의 "출처"만 바꾼다.
        self._hero_name_fixed = bool(hero_nickname.strip())
        self._hero_name = hero_nickname.strip()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

        # AI 호출(홀카드/보드)은 여기 제출하고 즉시 다음 폴링으로 넘어간다 -
        # 메인 루프가 AI 응답을 기다리지 않는다(task.md "99%를 위한 안정화" 3절).
        # worker 2개면 충분하다(홀카드 1회 + 보드 최대 3회/핸드, 동시에 여러 개
        # 몰려도 순차 처리되면서 메인 루프는 안 막힘).
        self._ai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dealer-chat-ai")

        # AI 보드 카드 인식은 스트리트당 한 번만 "제출"한다(성공/실패 무관, 다시
        # 시도하지 않는다 - task.md "과도한 호출 금지"). hand_no는 계속 증가만
        # 하므로 리셋 없이 멤버십만 확인해도 된다.
        self._board_attempted: set = set()  # {(hand_no, street), ...}

        # task.md "히어로 홀카드 인식 복구": 홀카드는 2장이 다 인식되거나(성공)
        # 시도 횟수를 다 쓸 때까지(포기) 재시도하므로, 보드와 달리 "이미 끝난 핸드"
        # 집합 + 핸드별 시도 횟수 + 현재 응답 대기 중인 핸드 집합을 따로 관리한다.
        self._hole_cards_resolved_hands: set = set()  # 2장 인식 완료 또는 포기로 끝난 핸드
        self._hole_cards_attempts: dict = {}  # hand_no -> 시도 횟수
        self._hole_cards_pending: set = set()  # 현재 AI 응답을 기다리는 중인 핸드(중복 제출 방지)

        # task.md "스트리트 헤더 보정(보드 장수 신호)": 현재 핸드에서 이미 찍은
        # 스트리트 헤더 집합(recorder가 텍스트로 찍었든, 아래 보드 장수 보정으로
        # 강제로 찍었든 상관없이 여기 다 모은다) - 새 핸드가 시작되면 비운다.
        self._cur_hand_no: int = 0
        self._cur_hand_board_count: int = 0
        self._cur_hand_headers: set = set()

        # task4.md "히어로 폴드 시 보드 확인도 함께 중단": 히어로가 폴드로 확정된
        # hand_no를 여기 모아둔다. 새 핸드는 매번 새 hand_no를 받으므로(증가만
        # 함) 별도 리셋 없이 멤버십만 확인해도 새 핸드는 항상 깨끗하게 시작된다
        # (지연/렉 없음 - task4.md "새 핸드 시작 시 느려지면 안 됨").
        self._folded_hands: set = set()

        # task.md "히어로 탈락 감지 및 기록 자동 중지": 경고+자동중지는 세션당
        # 딱 한 번만 트리거한다(반복 호출/중복 정지 방지).
        self._hero_missing_stop_triggered = False

    def start(self) -> None:
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # 이미 시작된 AI 호출은 그냥 끝까지 두고(취소하면 카드 결과를 영영 못
        # 받음), 아직 시작 안 한 것만 취소한다. 콜백은 스레드에서 계속 돌 수
        # 있으므로 on_line/on_status는 항상 예외를 삼키게 해뒀다(_emit_*).
        self._ai_executor.shutdown(wait=False, cancel_futures=True)

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

    def _emit_hero_missing(self) -> None:
        if self.on_hero_missing:
            try:
                self.on_hero_missing()
            except Exception:
                pass

    def _log_card_row(self, hand_no: int, street: str, label: str, cards: str) -> None:
        """홀카드/보드 카드도 기존 엑셀 컬럼 구조 그대로에 얹는다(새 컬럼 추가 안 함 -
        task.md "컬럼 구조는 기존 것을 최대한 유지"). "행동" 칸에 "홀카드"/"보드",
        "원문(OCR)" 칸에 실제 카드 문자열(또는 못 읽었으면 빈칸)을 넣는다."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        position = "나" if label == "홀카드" else ""
        row = [timestamp, hand_no, street, position, label, "", "예" if label == "홀카드" else "", cards]
        ok = self.xlsx_logger.append(row, highlight=(label == "홀카드"))
        if not ok:
            self._emit_status(self.xlsx_logger.last_error or "저장 실패")

    def _capture_table_img(self):
        if self.table_hwnd is None:
            return None
        return window_capture.capture_window(self.table_hwnd)

    def _read_hero_nickname(self, table_img) -> str:
        """테이블 창의 히어로 좌석(table_layout.json 기준, 하단 중앙 고정) 영역에서
        닉네임을 읽는다. 기존 메인 파이프라인(Recorder) 로직은 호출하지 않고,
        좌표 설정만 재사용한다. task.md 1절: 관전/실제 플레이 상관없이 항상 시도한다."""
        if table_img is None:
            return ""
        table_layout = load_table_layout()
        hero_seat = table_layout.hero_seat()
        if hero_seat is None:
            return ""
        region = resolve_ratio_region(hero_seat.region, table_img.size)
        try:
            crop = table_img.crop(region)
        except Exception:
            return ""
        for line in ocr_engine.recognize_lines(crop, lang=self.ocr_lang):
            text = line.text.strip()
            # 스택 액수("24.96" 등) 같은 숫자 줄은 닉네임이 아니므로 건너뛴다.
            if text and not text.replace(",", "").replace(".", "").isdigit():
                return text
        return ""

    # ---------- 홀카드 (비동기, 재시도) ----------
    def _submit_hero_cards(self, table_img, hand_no: int) -> None:
        """이 핸드가 아직 해결(2장 인식 또는 포기)되지 않았고, 지금 응답을 기다리는
        중이 아니고, 시도 횟수가 남아있으면 AI 호출을 스레드풀에 제출한다(즉시
        반환 - 메인 루프를 안 막음). 호출부(_run)가 매 폴링마다 불러도 안전하다 -
        해결/대기중/폴드/횟수초과면 그냥 조용히 넘어간다. 결과는
        _on_hero_cards_done에서 나중에 찍힌다."""
        if table_img is None:
            return
        if hand_no in self._hole_cards_resolved_hands or hand_no in self._folded_hands:
            return
        if hand_no in self._hole_cards_pending:
            return
        attempts = self._hole_cards_attempts.get(hand_no, 0)
        if attempts >= MAX_HOLE_CARD_ATTEMPTS:
            return
        self._hole_cards_attempts[hand_no] = attempts + 1
        self._hole_cards_pending.add(hand_no)

        table_layout = load_table_layout()
        region = resolve_ratio_region(table_layout.hole_cards_region, table_img.size)
        try:
            crop = table_img.crop(region)
        except Exception:
            crop = None

        future = self._ai_executor.submit(card_reader.read_hero_hole_cards, crop, f"hand{hand_no}_hole")
        future.add_done_callback(lambda f, hand_no=hand_no: self._on_hero_cards_done(f, hand_no))

    def _on_hero_cards_done(self, future, hand_no: int) -> None:
        self._hole_cards_pending.discard(hand_no)
        try:
            cards = future.result()
        except Exception:
            cards = ""

        if hand_no in self._folded_hands:
            # task4.md 관례와 동일: 제출 시점엔 안 폴드였어도 응답 도착 사이 히어로가
            # 폴드했으면 결과를 조용히 버린다(로그에 안 남김).
            self._hole_cards_resolved_hands.add(hand_no)
            return

        got_two_cards = len(cards.split()) >= 2
        attempts_left = self._hole_cards_attempts.get(hand_no, 0) < MAX_HOLE_CARD_ATTEMPTS
        if not got_two_cards and attempts_left:
            # 아직 2장을 다 못 읽었고 재시도 여지가 남아있으면 다음 폴링에서
            # _submit_hero_cards가 다시 시도한다(_run 참고) - 여기서는 중간 결과로
            # 로그를 어지럽히지 않기 위해 아직 줄을 찍지 않는다.
            return

        # 2장을 다 읽었거나(성공) 시도 횟수를 다 썼다(포기) - 이 핸드는 여기서 확정.
        self._hole_cards_resolved_hands.add(hand_no)
        # 관전 모드라 안 보이거나 끝내 다 못 읽으면 card_reader가 빈 문자열/부분
        # 결과를 반환하고, 그대로 "(미확인)"으로 표시한다(에러 아님 - task.md 3절).
        self._emit_line(f"내 홀카드: {cards or '(미확인)'}", "hand_header")
        self._log_card_row(hand_no, self._recorder.layout.streets[0], "홀카드", cards)

    # ---------- 보드 카드 (비동기) ----------
    def _street_header_text(self, table_img, street: str) -> str:
        """스트리트 헤더 줄(즉시 찍히는 부분)을 만든다. 프리플랍은 그냥
        "[프리플랍]", 플랍/턴/리버는 로컬 픽셀 판별 장수만 우선 붙인다
        ("[플랍] 보드: 3장") - AI가 읽은 실제 카드 값은 나중에 별도 줄로
        이어붙는다(_on_board_cards_done, task.md "99%를 위한 안정화" 3절
        "카드 값은 나중에 채워져도 됨")."""
        if street not in BOARD_STREET_COUNTS or table_img is None:
            return f"[{street}]"
        local_count = self._local_board_count(table_img)
        if local_count:
            return f"[{street}] 보드: {local_count}장"
        return f"[{street}]"

    def _local_board_count(self, table_img) -> int:
        table_layout = load_table_layout()
        board_region_px = resolve_ratio_region(table_layout.board_region, table_img.size)
        try:
            return board_reader.detect_card_count(table_img, board_region_px)
        except Exception:
            return 0

    def _recheck_board_count_burst(self) -> int:
        """task.md "올인 쇼다운 리버 누락 수정": 리버를 놓쳤을 위험이 있는 딱 그
        순간에만(호출부의 조건 참고) 테이블 화면을 아주 짧게 몇 번 더 캡처해서,
        그 사이에 리버가 잠깐 보였는지 확인한다. 가장 높게 관측된 장수를 반환한다
        (못 찾으면 이번에 새로 관측한 값 그대로, 즉 호출 전과 달라지지 않을 수도
        있음). 평소엔 호출 자체가 안 되는 분기라 일반 폴링 주기와는 무관하다."""
        best = 0
        for _ in range(BOARD_RECHECK_ATTEMPTS):
            img = self._capture_table_img()
            if img is not None:
                best = max(best, self._local_board_count(img))
                if best >= 5:
                    break
            time.sleep(BOARD_RECHECK_INTERVAL_SEC)
        return best

    def _emit_board_milestones(self, table_img, hand_no: int, headers: set, local_count: int) -> None:
        """local_count 기준으로 headers에 아직 없는 마일스톤 스트리트 헤더를 찍고
        AI 보드 인식을 제출한다. headers는 호출한 쪽이 들고 있는 "이 핸드에서 이미
        찍은 스트리트" 집합을 그대로 in-place로 채운다(정상 진행 분기와 리버
        재확인 분기가 이 로직을 공유한다)."""
        for count, street in BOARD_COUNT_MILESTONES:
            if local_count >= count and street not in headers:
                headers.add(street)
                self._emit_line(f"[{street}] 보드: {count}장", "street_header")
                self._submit_board_cards(table_img, hand_no, street)

    def _submit_board_cards(self, table_img, hand_no: int, street: str) -> None:
        expected_count = BOARD_STREET_COUNTS.get(street)
        if expected_count is None or table_img is None:
            return
        # task4.md: 히어로가 이 핸드에서 이미 폴드했으면 보드 AI 호출 자체를 새로
        # 시작하지 않는다.
        if hand_no in self._folded_hands:
            return
        key = (hand_no, street)
        if key in self._board_attempted:
            return
        self._board_attempted.add(key)

        table_layout = load_table_layout()
        board_region_px = resolve_ratio_region(table_layout.board_region, table_img.size)
        local_count = self._local_board_count(table_img)
        try:
            crop = table_img.crop(board_region_px)
        except Exception:
            crop = None

        future = self._ai_executor.submit(
            card_reader.read_board_cards, crop, local_count or expected_count, f"hand{hand_no}_{street}"
        )
        future.add_done_callback(
            lambda f, hand_no=hand_no, street=street: self._on_board_cards_done(f, hand_no, street)
        )

    def _on_board_cards_done(self, future, hand_no: int, street: str) -> None:
        # task4.md "이미 진행 중인 보드 AI 요청이 있으면 결과를 버리거나 무시":
        # 제출 시점엔 안 폴드였어도, 응답이 도착하는 사이 히어로가 폴드했을 수
        # 있다 - 그 사이 값이면 결과를 그냥 버린다(로그/엑셀에 안 남김).
        if hand_no in self._folded_hands:
            return
        try:
            cards = future.result()
        except Exception:
            cards = ""
        # task.md "로그 안정화 4종" 3절: 장수는 AI가 실제로 읽은 개수를 그대로
        # 쓴다(스트리트 이름에 억지로 안 맞춘다). AI가 아예 못 읽었으면 조용히
        # 넘어간다 - 스트리트 헤더 찍을 때 이미 로컬 판별 장수를 보여줬으므로
        # "0장" 같은 줄을 새로 찍어서 혼란을 더할 필요는 없다.
        if cards:
            count = len(cards.split())
            self._emit_line(f"[{street}] 보드 확인: {count}장 ({cards})", "street_header")
        self._log_card_row(hand_no, street, "보드", cards)

    def _log_to_xlsx(self, row, hand_no: int) -> None:
        # xlsx_writer.HEADERS 순서: 시간/게임 번호/스트리트/포지션/행동/금액/내 행동/원문(OCR)
        # - 기존 recorder.py의 행 구성과 동일한 순서를 그대로 따른다(task.md [E]
        # "컬럼 구조는 기존 것을 최대한 유지").
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        xlsx_row = [
            timestamp, hand_no, row.street, row.position, row.action,
            row.amount, "예" if row.is_hero else "", row.raw_text,
        ]
        ok = self.xlsx_logger.append(xlsx_row, highlight=row.is_hero)
        if not ok:
            self._emit_status(self.xlsx_logger.last_error or "저장 실패")

    def _run(self) -> None:
        # task.md "기록 시작 동기화(깔끔한 진입)": 기록 시작 직후엔 화면에 이미
        # 진행 중이던 핸드 조각이 보일 수 있으므로, DealerChatRecorder가 진짜 새
        # 핸드 경계를 잡을 때까지(또는 타임아웃) 동기화 중임을 상태 문구로 알린다.
        self._emit_status("동기화 중... 다음 핸드부터 기록")
        while not self._stop_flag.is_set():
            start_t = time.time()
            chat_img = window_capture.capture_window(self.chat_hwnd)
            if chat_img is None:
                self._emit_status("딜러 채팅 창을 캡처할 수 없습니다(최소화 여부 확인).")
                self._stop_flag.wait(self.interval_sec)
                continue

            # 테이블 창은 한 번만 캡처해서 닉네임/홀카드/보드에 재사용한다(각자
            # 따로 캡처하면 3배 느려짐). table_hwnd가 없으면 그냥 None으로,
            # 아래 각 함수들은 None을 받으면 빈 값으로 조용히 넘어간다.
            table_img = self._capture_table_img()
            if table_img is not None and not self._hero_name_fixed:
                # 설정값이 있으면 이 좌석 OCR 재탐지 자체를 건너뛴다(task.md 1절
                # "매 폴링마다 좌석 OCR로 닉네임을 다시 찾지 않음"). table_img는
                # 홀카드/보드 인식에 그대로 계속 쓰인다.
                nickname = self._read_hero_nickname(table_img)
                if nickname:
                    self._hero_name = nickname

            # task.md "올인 쇼다운 리버 누락 수정": 아래 for 루프가 같은 폴링 안에서
            # 새 핸드를 감지하면 self._cur_hand_no/_cur_hand_board_count/
            # _cur_hand_headers를 그 새 핸드 기준으로 바로 갱신해버린다(아래).
            # 그러면 "직전 핸드가 리버를 못 보고 넘어갔는지" 판단할 기준이 사라지므로,
            # for 루프를 돌기 전 지금 값을 따로 남겨둔다.
            prev_hand_no = self._cur_hand_no
            prev_hand_board_count = self._cur_hand_board_count
            prev_hand_headers = self._cur_hand_headers

            # task.md "99%를 위한 안정화" 3절: 이 for 루프 전체가 딜러 채팅 OCR/
            # 파싱 결과만 다루고, AI 호출은 아래에서 스레드풀에 "제출"만 하고
            # 끝난다 - AI 응답을 기다리는 코드가 이 루프 안에 전혀 없다.
            was_syncing = self._recorder.syncing
            for item in self._recorder.process_frame(chat_img, hero_name=self._hero_name):
                if item.tag == "hand_header" and item.row is None:
                    # task.md "스트리트 헤더 보정": recorder가 여기서 만드는
                    # hand_header 줄은 항상 "[N게임 시작]"뿐이다 - 새 핸드가
                    # 시작됐으니 보드 장수 보정 상태를 이 핸드 기준으로 새로 잡는다.
                    self._cur_hand_no = item.hand_no
                    self._cur_hand_board_count = 0
                    self._cur_hand_headers = set()

                if item.tag == "street_header":
                    street_name = item.text.strip("[]")
                    if street_name in self._cur_hand_headers:
                        # 딜러 채팅 텍스트가 뒤늦게 나타난 경우 - 이미 보드 장수
                        # 보정으로 먼저 찍은 헤더라 중복 출력하지 않는다.
                        continue
                    self._cur_hand_headers.add(street_name)
                    text = self._street_header_text(table_img, street_name)
                    self._emit_line(text, item.tag)
                    self._submit_board_cards(table_img, item.hand_no, street_name)
                    continue

                self._emit_line(item.text, item.tag)
                if item.row is not None:
                    self._log_to_xlsx(item.row, item.hand_no)
                # 홀카드 첫 시도는 이 for 루프가 끝난 뒤 self._cur_hand_no 기준
                # 재시도 블록에서 이뤄진다(task.md 4절 포맷: "[N게임 시작]" 다음
                # 줄에 "내 홀카드: ..."는 비동기 콜백이 나중에 이어붙인다).

            # task4.md "히어로 폴드 시 보드 확인도 함께 중단": recorder가 히어로
            # 폴드를 확정한 순간(_hero_folded_this_hand) 이 핸드 번호를 기록해
            # 둔다. process_frame은 폴드 처리 직후 그 핸드 동안은 더 이상 아무
            # 항목도 내보내지 않으므로(위 for 루프가 매 폴링 비어있게 됨), 아래
            # 로컬 보드 장수 보정 블록이 유일하게 남는 누출 경로였다.
            if self._recorder._hero_folded_this_hand:
                self._folded_hands.add(self._cur_hand_no)

            # task.md "히어로 탈락 감지 및 기록 자동 중지": 히어로 닉네임을 사용자가
            # 직접 설정했을 때만 판단 대상이다(좌석 OCR 자동 인식 모드는 자리 주인이
            # 바뀌면 그 사람 이름을 그대로 따라가버려서 "탈락"이라는 개념 자체가
            # 성립하지 않는다). 관전 모드/일시적 인식 실패와 구분하기 위해 한 핸드가
            # 아니라 연속 여러 핸드(HERO_MISSING_HAND_THRESHOLD) 동안 전혀 안 보일
            # 때만 판단한다 - process_frame이 핸드 경계마다 갱신해주는 스트릭을 그대로
            # 확인만 한다(판단 로직 자체는 recorder에 있음).
            if (
                self._hero_name_fixed
                and not self._hero_missing_stop_triggered
                and self._recorder.hero_missing_streak >= HERO_MISSING_HAND_THRESHOLD
            ):
                self._hero_missing_stop_triggered = True
                warning = "히어로가 테이블에서 보이지 않습니다. 기록을 중지합니다."
                self._emit_line(warning, "result")
                self._emit_status(warning)
                self._emit_hero_missing()
                self._stop_flag.set()

            # task.md "히어로 홀카드 인식 복구": 핸드 시작 순간의 캡처 한 번으로는
            # 딜링 애니메이션 등 때문에 카드가 아직 안 보일 수 있어서, 이 핸드가
            # 해결(2장 인식 또는 포기)될 때까지 매 폴링마다 다시 시도한다 -
            # _submit_hero_cards 내부에서 이미 해결/폴드/대기중/횟수초과면 알아서
            # 건너뛰므로 매 프레임 불러도 안전하다. 방금 히어로 탈락으로 판단해
            # 중지를 트리거했으면(위) 의미 없는 AI 호출을 한 번 더 안 하도록 건너뛴다.
            if table_img is not None and self._cur_hand_no and not self._stop_flag.is_set():
                self._submit_hero_cards(table_img, self._cur_hand_no)

            local_count = self._local_board_count(table_img) if table_img is not None else 0

            # task.md "올인 쇼다운 리버 누락 수정": 직전 핸드가 리버(5장)를 아직 못
            # 봤는데(플랍/턴까지만, 3~4장) - (a) 같은 폴링 안에서 이미 새 핸드로
            # 넘어갔거나, (b) 이번에 새로 읽은 장수가 직전 값보다 줄어들어 보이면
            # (둘 다 "쇼다운이 폴링 주기보다 빨리 지나가서 놓쳤을 수 있다"는 신호),
            # 화면을 아주 짧게 다시 몇 번 더 봐서 리버가 잠깐 남아있었는지 확인한다.
            # 정상적으로 리버까지 이미 본 핸드나 보드가 아직 3/4장이 아닌 핸드는
            # 이 분기 자체를 안 타므로, 일반 핸드/새 핸드 시작 속도에는 영향 없다.
            hand_transitioned = prev_hand_no != self._cur_hand_no
            missed_river_risk = (
                prev_hand_no
                and prev_hand_no not in self._folded_hands
                and prev_hand_board_count in (3, 4)
                and "리버" not in prev_hand_headers
                and (hand_transitioned or local_count < prev_hand_board_count)
            )
            if table_img is not None and missed_river_risk:
                recheck_count = self._recheck_board_count_burst()
                if recheck_count > prev_hand_board_count:
                    self._emit_board_milestones(table_img, prev_hand_no, prev_hand_headers, recheck_count)
                    if not hand_transitioned:
                        # 아직 같은 핸드면(새 핸드로 안 넘어감) 정상 진행 블록이 이
                        # 값을 기준으로 계속 진행하게 갱신한다. 새 핸드로 이미
                        # 넘어갔으면 self._cur_hand_board_count는 그 새 핸드용으로
                        # 이미 0으로 리셋돼 있으므로 여기서 건드리지 않는다.
                        self._cur_hand_board_count = recheck_count
                        local_count = recheck_count

            if (
                table_img is not None
                and self._cur_hand_no
                and not self._recorder.syncing
                and self._cur_hand_no not in self._folded_hands
            ):
                # task.md "스트리트 헤더 보정(보드 장수 신호)": 딜러 채팅에 행동
                # 텍스트가 하나도 없는 스트리트(예: 전원 체크로 넘어간 플랍)는 위
                # 루프에서 recorder가 street_header를 절대 안 준다 - 로컬 보드 장수
                # 증가를 독립 신호로 써서 그런 헤더도 강제로 채운다. 장수가
                # 줄어들거나 같으면 무시하고(아래 비교), 행동 줄은 만들지 않는다.
                if local_count > self._cur_hand_board_count:
                    self._emit_board_milestones(table_img, self._cur_hand_no, self._cur_hand_headers, local_count)
                    self._cur_hand_board_count = local_count

            if was_syncing and not self._recorder.syncing:
                # task.md "기록 시작 동기화" 4절: 동기화가 막 끝났다 - 실패로
                # 끝났으면(타임아웃) 그 사실을 먼저 알리고, 이어서 평소 상태 문구로
                # 되돌린다.
                if self._recorder.sync_timed_out:
                    self._emit_status("동기화 실패 - 로컬 모드로 진행")
                self._emit_status("딜러 채팅 기록 중")

            elapsed = time.time() - start_t
            self._stop_flag.wait(max(0.0, self.interval_sec - elapsed))

        self._emit_status("기록이 중지되었습니다.")
