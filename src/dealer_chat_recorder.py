"""딜러 채팅 실시간 기록기 (task.md "딜러 채팅 파서 안정화 + 메인 연결 준비").

dealer_chat_parser.py는 캡처 한 장을 받아 그 순간 보이는 내용을 통째로 파싱하는
단발성 함수만 제공한다. 이 모듈은 그걸 실시간 폴링 루프에서 쓸 수 있게 감싸서:

  [A] 포지션 OCR 누락 보완 - 이번 핸드에서 그 플레이어 포지션을 이미 한 번이라도
      읽은 적 있으면 즉시 재사용한다(포지션은 핸드 내내 안 바뀌므로).
      task.md "99%를 위한 안정화" 1절: 재사용 조회를 이름 완전일치에서 접두어
      매칭으로 넓혔다(hero 매칭에 쓰던 것과 같은 방식) - 말줄임표로 잘린 이름이
      폴링마다 조금씩 다르게 OCR돼도("G0r1ll4Glu3" vs "G0r1ll4Gl") 같은 사람으로
      보고 이미 배운 포지션을 재사용한다.
        task.md "액션 순서 뒤섞임 수정"(2026-08-21): 예전엔 재사용으로도 못 채운
      포지션은 출력을 몇 프레임 미루면서 재시도했는데, 그 사이 다른 행이 먼저
      나가면서 로그 순서가 실제 딜러 채팅 순서와 달라지는 문제가 있었다(실측
      확인: 지연됐던 행이 한참 뒤 다른 올인 액션들 사이에 끼어서 출력됨). 이제는
      재사용으로도 못 채우면 그 자리에서 바로 이름 접두어로 폴백해서 출력한다 -
      정확도(포지션 배지를 한 프레임 놓쳐도 재시도로 건졌던 것)보다 순서 정확성을
      우선한다. 어차피 포지션을 못 읽어도 줄 자체는 항상 남는다(이름 접두어 폴백,
      아래 [E] 참고).
  [D] 새 핸드 / 스트리트 감지 - 아래 신호들을 종합해서 판단한다:
      1) 프리플랍 컬럼에서 "지금까지 이번 핸드에서 본 적 있는 줄"이 이번
         폴링에 하나도 안 보임(패널이 리셋되고 다음 핸드 안티가 새로 쌓임).
         처음엔 "첫 줄이 바뀌면"으로 시도했는데, 폴드가 워낙 흔한 행동이라
         서로 다른 핸드의 첫 줄이 우연히 같아지는 경우가 잦아(예: 두 핸드 모두
         "UTG Fold") 오탐이 났다 - "본 줄 전체 집합과 하나도 안 겹치는지"로
         바꿔서 없앴다.
      2) 안티는 그 핸드에서 제일 먼저 나온다는 시간 순서 규칙 - 안티가 아닌
         행동을 이미 하나라도 봤는데 처음 보는 안티 줄이 또 보이면 새 핸드.
      3) (신규) 폴드도 그 핸드 안에서는 되돌릴 수 없다는 규칙 - 이미 폴드로
         기록된 플레이어 이름으로 또 다른(새로운) 줄이 보이면(폴드를 다시
         하든 다른 행동을 하든) 새 핸드. 프리플랍에서 짧게 끝나는 핸드가
         연달아 나올 때(task.md "0→0 핸드") 1)만으로는 겹치는 폴드가 우연히
         일치해서 놓치는 경우가 있어 추가했다.
      4) 프리플랍 컬럼이 연속 2폴링 이상 비어 보이다가 다시 채워지면 새 핸드로
         본다 - 실제로 패널이 리셋되는 게 보였는데도 겹침 검사만으로는 못 잡는
         경우의 보강.
           (task.md "패널 리셋 오탐 수정"(2026-08-20) 회귀 수정: 처음엔 "1번이라도
         비면" 바로 인정했는데, 실측 영상에서 OCR이 딱 한 프레임만 컬럼 전체를
         못 읽은 것뿐인데도 "패널 리셋"으로 오판해서 한 핸드가 "UTG Fold" 한
         줄마다 다음 게임으로 잘리는 회귀가 났다(게임 번호가 4→5→6→7로 연쇄
         쪼개짐). 연속 2폴링 이상 비었을 때만 인정하도록 강화했다 - 폴링 주기가
         1.5초 안팎이니 2폴링이면 자연스럽게 1초 이상 지속된 경우만 걸러진다.)
      1)~4) 중 하나라도 걸리면 "새 핸드 신호"로 본다. 단, task.md "핸드 쪼개짐
      수정"(2026-08-21): 이 신호를 본 즉시 확정하지 않는다 - 6인 테이블처럼
      딜러 채팅 패널이 스크롤되는 UI에서는 스크롤 중 순간적인 OCR 흔들림이
      1)~4) 중 하나를 우연히 충족시켜서, 같은 핸드인데도 [2게임 시작],
      [3게임 시작]이 연속으로 찍히는 문제가 실측 확인됐다(같은 핸드 안에서
      스택/팟이 그대로인데 히어로 액션 한 번에 게임 번호가 튐). 그래서
      preflop_empty_confirm_streak과 같은 패턴으로, 신호가 연속 몇 폴링
      이상(new_hand_confirm_streak) 이어져야만 진짜 새 핸드로 확정한다 - 신호가
      중간에 끊기면(다음 폴링엔 다시 겹침) 그냥 원래 핸드로 취급하고 스트릭만
      리셋한다(그 폴링 내용은 정상적으로 현재 핸드에 합류). 단, "이번이 이
      기록 세션의 첫 관측"(not self._started)은 바로 확정한다 - 처음 시작할 때는
      기다릴 대상 자체가 없다. 각 행동은 이미 dealer_chat_parser가 어느
      컬럼(스트리트)에서 나왔는지 같이 주므로, 스트리트 헤더는 그 스트리트의
      첫 행동이 나올 때 한 번만 찍는다.
  [E] 기존 결과 포맷 - [N게임 시작] / [스트리트] / "나 : " 형식으로 한 줄씩 만들어
      반환한다. 히어로가 폴드하면 그 핸드는 더 이상 새 줄을 만들지 않는다.

task.md "로그 안정화 4종"(2026-08-20) 반영 사항:
  - Ante는 내부적으로는 여전히 인식한다(새 핸드 감지에 필요 - 위 [D] 참고)하고
    포지션을 미리 배워두는 데도 쓰지만, 로그/엑셀에는 절대 남기지 않는다
    (_emit_row 이전에 걸러낸다).
  - 중복 방지 키를 이름 기반 하나에서 "이름 기반" + "포지션 기반" 두 개로
    늘렸다. 이름이 폴링마다 OCR로 살짝 다르게 잡혀도(말줄임표 흔들림 등), 같은
    스트리트에서 같은 포지션이 같은 행동을 같은 금액으로 또 하면 중복으로 보고
    버린다(실측 확인: 이름 기반 키만으로는 이 경우를 못 걸렀다).
  - 인원수 표기("(N인 테이블)")는 뒤죽박죽인 게 실측으로 재확인돼서 아예
    없앴다(task.md 4절 A안).

AI(홀카드/보드) 호출은 이 클래스가 아니라 dealer_chat_bridge.py에서 비동기로
처리한다(task.md "99%를 위한 안정화" 3절) - 여기(process_frame)는 딜러 채팅
OCR/파싱/새핸드감지만 하고 AI와는 완전히 무관하게 항상 즉시 끝난다.

기존 메인 파이프라인(recorder.py)의 아우라/밝기/딜러버튼 로직은 그대로 두고,
여기서는 전혀 참조하지 않는다 - 딜러 채팅 전용 독립 상태 기계다.
"""
from dataclasses import dataclass, field

from PIL import Image

from . import dealer_chat_parser
from .dealer_chat_parser import ChatRow, DealerChatLayout

# 아래 값(패널 리셋 확정/동기화 타임아웃)은 전부 "폴링 몇 번"이 아니라 "실제로
# 몇 초 기다릴지"가 진짜 의도다 - 프레임 수로 박아두면 폴링 주기(interval_sec)를
# 나중에 바꿀 때마다 실제 대기 시간이 같이 늘었다 줄었다 해서 조용히 깨진다.
# 그래서 목표를 "초" 단위로 정의해두고, __post_init__에서 실제 interval_sec에
# 맞춰 프레임 수로 환산한다.
PREFLOP_EMPTY_CONFIRM_TARGET_SEC = 1.6  # 기존 PREFLOP_EMPTY_CONFIRM_STREAK(2) * 0.8 기준
# task.md "핸드 쪼개짐 수정": 권장대로 "연속 2회" 수준 - 기본 interval_sec(0.2초)
# 기준 약 0.4초(2폴링)다. preflop_empty_confirm_streak(1.6초)보다 훨씬 짧게 둔
# 이유는 진짜 새 핸드는 최대한 빨리 감지돼야 하기 때문(요구사항 "너무 늦지 않게
# 감지") - 스크롤 중 순간적인 OCR 흔들림 한두 프레임만 걸러내면 충분하다.
NEW_HAND_CONFIRM_TARGET_SEC = 0.4
SYNC_TIMEOUT_TARGET_SEC = 60.0  # task.md "기록 시작 동기화" 4절 목표치(대략 1분) 그대로

HERO_LABEL = dealer_chat_parser.HERO_LABEL


def _norm_name(name: str) -> str:
    return dealer_chat_parser._normalize_name(name)


# 포지션 재사용 조회용 접두어 매칭 최소 길이 - 히어로 이름 접두어 매칭(task.md
# [D]/dealer_chat_parser.names_match_prefix)과 같은 기준을 쓴다.
NAME_MATCH_MIN_LEN = 5


def _names_match(a: str, b: str, min_len: int = NAME_MATCH_MIN_LEN) -> bool:
    """a, b는 이미 정규화된 이름. 말줄임표로 잘린 이름이 폴링마다 조금씩 다르게
    OCR돼도(예: "G0r1ll4Glu3" vs "G0r1ll4Gl") 접두어가 겹치면 같은 사람으로 본다.

    task.md "? 포지션 재사용 강화": 접두어 매칭만으로는 이름이 잘린 게 아니라
    글자 하나가 다르게 읽힌 경우(예: "Vincy" vs "Vincv")를 못 잡는다 - 히어로
    매칭(dealer_chat_parser.names_match_prefix)에 쓰던 편집거리-1 허용을 포지션
    재사용 조회에도 그대로 가져온다. 히어로 매칭 알고리즘 자체는 안 건드리고,
    그게 쓰던 유틸 함수만 재사용한다."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < min_len:
        return False
    if longer.startswith(shorter):
        return True
    return abs(len(a) - len(b)) <= 1 and dealer_chat_parser._within_edit_distance_one(a, b)


# task.md "나(위치) 표시 + ? 완화" 2절B: 포지션을 못 읽은 일반 플레이어 줄에서
# "?" 대신 보여줄 이름 접두어 길이.
NAME_PREFIX_DISPLAY_LEN = 6


def _display_name_prefix(name: str, length: int = NAME_PREFIX_DISPLAY_LEN) -> str:
    """포지션이 끝내 안 잡혔을 때 "? ]" 대신 보여줄 이름 접두어(task.md 2절B).
    말줄임표(OCR이 "•"로 읽는다)나 그 외 잡음 문자는 지우고 앞 length자만 남긴다.
    그러고도 남는 게 거의 없으면(이름 자체를 거의 못 읽은 극소수 경우) "?"로
    되돌린다(task.md 2절C) - 행동 줄 자체는 이 경우에도 그대로 출력된다."""
    cleaned = dealer_chat_parser._NON_ALNUM.sub("", name or "")
    if len(cleaned) < 2:
        return "?"
    return cleaned[:length]


@dataclass
class EmittedLine:
    """process_frame이 이번 폴링에서 새로 확정한 한 줄. text/tag는 콘솔·로그창
    출력용, row는 엑셀 등 구조화된 저장이 필요할 때 쓴다(hand_header/street_header
    줄은 특정 행동에 대응하지 않으므로 row=None)."""
    text: str
    tag: str  # hand_header/street_header/action/hero_action
    row: ChatRow | None = None
    hand_no: int = 0


@dataclass
class DealerChatRecorder:
    layout: DealerChatLayout = field(default_factory=dealer_chat_parser.load_layout)
    ocr_lang: str = "ko"
    # 실제 폴링 주기(초) - PREFLOP_EMPTY_CONFIRM_TARGET_SEC 등 "초" 단위 목표를
    # 프레임 수로 환산하는 데 쓴다(__post_init__). main.py/DealerChatBridge가 쓰는
    # interval_sec과 같은 값을 넘겨줘야 한다.
    interval_sec: float = 0.8

    hand_no: int = 0
    _started: bool = False
    _hand_preflop_keys: set = field(default_factory=set)  # (norm(name),action,amount) - 이번 핸드 프리플랍에서 본 줄 전체
    _hand_has_decisive_action: bool = False  # 이번 핸드에 안티 아닌 행동을 하나라도 봤는지
    _hand_player_positions: dict = field(default_factory=dict)  # norm(name) -> position
    _hand_folded_names: set = field(default_factory=set)  # 이번 핸드에 폴드로 확정된 norm(name)
    # task.md "참가인원(추정) 표시": 이번 핸드에서 안티를 낸 사람들(norm name).
    # 안티는 앉아있는 모든 좌석이 예외 없이 낸다는 게 실측 확인됐다(SB/BB도
    # 블라인드와 별개로 안티를 낸다) - 다만 100% 보장은 아니라서(OCR이 한 명의
    # 안티 줄을 놓칠 수 있음) "확정"이 아니라 "추정치"로만 쓴다. 안티가 없는
    # 게임(포맷 자체가 다름)에서는 이 집합이 계속 비어있고, 그런 핸드는 추정치
    # 자체를 안 남긴다(아래 _begin_new_hand 참고).
    _hand_ante_names: set = field(default_factory=set)
    # task.md "중복 기록 수정": 포지션이 아직 한 번도 안 잡힌 플레이어의 행이
    # (street, action, amount) 별로 어떤 이름들로 찍혔는지 모아둔다. 같은 사람의
    # 이름이 폴링마다 조금씩 다르게 OCR돼도(예: "걸bshed" vs "bsheds") 포지션이
    # 아직 안 잡혀서 포지션 기반 중복 방지(_emitted_position_keys)가 못 걸렀던
    # 경우를, 이름 퍼지 매칭으로 대신 잡는다.
    _hand_unresolved_action_names: dict = field(default_factory=dict)  # (street,action,amount) -> [norm(name), ...]
    # 실측 확인(2026-08-21, 5개 영상 교차 검증): 같은 사람의 같은 행동이 한 poll엔
    # 금액이 정확히 읽히고(예: "Call 400") 바로 다음 poll엔 금액 OCR만 깨져서
    # (예: "예"를 "00"으로 오인식해 "400"→"4예", 파싱 결과 금액 없음) 다시 찍히는
    # 사례를 확인했다 - 위 _emitted_position_keys는 금액까지 완전히 같아야 걸러지므로
    # 이 경우를 못 잡는다. "바로 직전에 찍힌 행동"(같은 스트리트, 같은 포지션, 같은
    # 행동)만 기억해뒀다가, 금액만 다르게 또 나오면 중복으로 보고 버린다 - 그 사이에
    # 다른 사람 행동이 하나라도 끼면 자동으로 갱신되므로, 한 사람이 같은 스트리트에서
    # 정말로 두 번 액션하는 정상적인 경우(재레이즈 등)와는 섞이지 않는다.
    _last_emitted_ident_action: tuple | None = None  # (street, position, action) or None
    _preflop_empty_streak: int = 0  # 프리플랍 컬럼이 내용 있다가 연속으로 비어 보인 폴링 횟수
    # task.md "핸드 쪼개짐 수정": 새 핸드 신호(안티 재등장/폴드 재등장/패널
    # 리셋/겹침 없음 중 하나라도)가 연속으로 몇 폴링째 이어지고 있는지. 신호가
    # 끊기면(같은 폴링 안에서라도) 즉시 0으로 리셋된다 - 한 번의 스냅샷만 보고
    # 바로 확정하지 않기 위한 디바운스 카운터.
    _pending_new_hand_streak: int = 0
    _emitted_keys: set = field(default_factory=set)  # (street, norm(name), action, amount)
    _emitted_position_keys: set = field(default_factory=set)  # (street, position, action, amount)
    _street_started: set = field(default_factory=set)  # 이번 핸드에 헤더 찍은 스트리트
    _hero_folded_this_hand: bool = False

    # task.md "히어로 탈락 감지 및 기록 자동 중지": 이번 핸드에서 히어로 닉네임과
    # 매칭되는 행(안티 포함, ChatRow.is_hero)을 한 번이라도 봤는지. 새 핸드가
    # 시작될 때(_begin_new_hand) 직전 핸드 기준으로 확인한 뒤 리셋한다.
    _hero_seen_this_hand: bool = False
    # 히어로가 연속으로 몇 핸드째 전혀 안 보이는지. 정상적으로 한 번이라도
    # 보이면 0으로 리셋된다 - dealer_chat_bridge.py가 이 값을 보고 일정 횟수
    # 이상이면 탈락으로 판단해 경고 + 기록 중지를 트리거한다.
    hero_missing_streak: int = 0

    # task.md "참가인원(추정) 표시": 방금 끝난 핸드의 안티 낸 사람 수(추정
    # 참가인원)와 그 핸드 번호 - dealer_chat_bridge.py가 이 hand_no를 보고
    # (아직 안 내보냈으면) "참가인원(추정): N명" 한 줄을 카드 상세 채널로
    # 내보낸다. 안티가 하나도 없던 핸드(안티 없는 게임 등)는 last_participant_hand_no
    # 가 갱신되지 않으므로 그 핸드는 추정치 자체가 안 나간다.
    last_participant_count: int = 0
    last_participant_hand_no: int = 0

    # task.md "기록 시작 동기화(깔끔한 진입)": 기록을 막 시작해서 화면에 이미 진행
    # 중이던 핸드 조각이 보이는 동안엔 True. 이 상태에선 일반 행동 로그를 출력하지
    # 않고, 진짜 새 핸드 경계가 잡히는 순간(또는 타임아웃) 딱 한 번만 꺼진다.
    syncing: bool = True
    sync_timed_out: bool = False  # 타임아웃으로 강제 종료됐는지(상태 문구용)
    _sync_polls: int = 0

    def __post_init__(self) -> None:
        # "초" 목표를 실제 interval_sec 기준 프레임 수로 환산(최소 1) - 폴링
        # 주기가 바뀌어도 실제 대기/타임아웃 시간이 그대로 유지된다.
        sec = self.interval_sec if self.interval_sec > 0 else 0.8
        self.preflop_empty_confirm_streak = max(1, round(PREFLOP_EMPTY_CONFIRM_TARGET_SEC / sec))
        self.new_hand_confirm_streak = max(1, round(NEW_HAND_CONFIRM_TARGET_SEC / sec))
        self.sync_timeout_polls = max(1, round(SYNC_TIMEOUT_TARGET_SEC / sec))

    def _row_key(self, row: ChatRow) -> tuple:
        # 이름은 정규화해서 키로 쓴다 - 말줄임표 붙은 이름은 폴링마다 OCR이 점(•)
        # 개수를 다르게 읽는 경우가 실측 확인돼서("M0mSp4gh•••" vs "M0mSp4gh••"),
        # 원문 그대로 키를 만들면 같은 행동이 중복 출력된다.
        return (row.street, _norm_name(row.player), row.action, row.amount)

    def _resolve_position(self, row: ChatRow) -> str:
        """이번 핸드에서 이 플레이어 포지션을 이미 알고 있으면 그걸 쓴다(포지션은
        핸드 내내 고정이므로 - task.md [A]). 완전일치가 없으면 접두어 매칭으로도
        찾아본다(task.md "99%를 위한 안정화" 1절 - 말줄임표 이름 OCR 흔들림 보완)."""
        name = _norm_name(row.player)
        if row.position != "?":
            self._hand_player_positions[name] = row.position
            return row.position
        known = self._hand_player_positions.get(name)
        if known:
            return known
        for known_name, pos in self._hand_player_positions.items():
            if _names_match(known_name, name):
                return pos
        return "?"

    def _format_action(self, row: ChatRow, position: str) -> str:
        if row.is_hero:
            # task.md "나(위치) 표시 + ? 완화" 1절: 포지션을 알면 괄호로 같이
            # 보여준다. 못 읽었으면 기존 "나 :" 그대로(대기 줄은 넣지 않음).
            if position != "?":
                text = f"{HERO_LABEL} ({position}) : {row.action}"
            else:
                text = f"{HERO_LABEL:<5}: {row.action}"
        else:
            # task.md 2절: 포지션이 끝내 안 잡히면 "? ]" 대신 이름 접두어를
            # 보여준다(재사용/재시도까지 다 실패한 경우에만 여기로 옴). 이름 자체를
            # 거의 못 읽은 극소수만 "?"로 남는다(_display_name_prefix가 처리).
            display = position if position != "?" else _display_name_prefix(row.player)
            text = f"{display:<5} ] {row.action}"
        if row.amount:
            text += f" {row.amount}"
        return text

    def _reset_hand_state(self) -> None:
        """핸드 하나치 상태를 전부 비운다(hand_no는 건드리지 않음) - 정식으로
        새 핸드를 여는 경로(_begin_new_hand)와, 동기화 중 베이스라인만 다시 잡는
        경로(process_frame) 둘 다에서 쓴다."""
        self._hand_player_positions = {}
        self._hand_folded_names = set()
        self._hand_ante_names = set()
        self._hand_unresolved_action_names = {}
        self._hand_has_decisive_action = False
        self._hand_preflop_keys = set()
        self._emitted_keys = set()
        self._emitted_position_keys = set()
        self._street_started = set()
        self._hero_folded_this_hand = False
        self._hero_seen_this_hand = False
        self._pending_new_hand_streak = 0
        self._last_emitted_ident_action = None

    def _begin_new_hand(self, hero_name: str = "") -> str:
        # task.md "히어로 탈락 감지 및 기록 자동 중지": 방금 끝난 핸드 기준으로
        # 연속 미등장 횟수를 갱신한다 - _reset_hand_state가 _hero_seen_this_hand를
        # 다시 False로 비우기 전에 확인해야 한다. 관전 모드처럼 히어로 닉네임
        # 자체가 없는 세션(hero_name이 빈 문자열)에서는 판단 대상이 아니므로
        # 건드리지 않는다(dealer_chat_bridge.py도 이 경우 스트릭을 안 본다).
        if hero_name:
            if self._hero_seen_this_hand:
                self.hero_missing_streak = 0
            else:
                self.hero_missing_streak += 1
        # task.md "참가인원(추정) 표시": 방금 끝난 핸드에서 안티를 낸 사람 수를
        # 그 핸드의 참가인원 추정치로 남겨둔다 - _reset_hand_state가
        # _hand_ante_names를 비우기 전에 확인해야 한다. 안티가 하나도 없으면
        # (안티 없는 게임 포맷 등) 남기지 않는다 - "0명" 같은 의미없는 추정치를
        # 보여주지 않기 위해서다.
        if self._hand_ante_names:
            self.last_participant_count = len(self._hand_ante_names)
            self.last_participant_hand_no = self.hand_no
        # task.md "로그 안정화 4종" 4절 A안: 인원수 표기가 실측으로 계속
        # 뒤죽박죽인 게 확인돼서 아예 없앴다("[N게임 시작]"만 남김).
        self.hand_no += 1
        self._reset_hand_state()
        return f"[{self.hand_no}게임 시작]"

    def process_frame(self, chat_img: Image.Image, hero_name: str = "") -> list[EmittedLine]:
        """캡처 이미지 한 장을 처리해서, 이번 폴링에서 새로 확정된 줄들을
        EmittedLine 목록으로 반환한다. 태그는 hand_header/street_header/action/
        hero_action 중 하나 - main.py의 로그 창 색상 태그와 형식을 그대로 맞춰뒀다."""
        parsed = dealer_chat_parser.parse_dealer_chat(
            chat_img, layout=self.layout, hero_name=hero_name, ocr_lang=self.ocr_lang
        )
        out: list[EmittedLine] = []

        if self.syncing:
            self._sync_polls += 1

        preflop_street = self.layout.streets[0]
        preflop_rows = parsed.get(preflop_street) or []

        if not preflop_rows:
            # 프리플랍 컬럼이 (내용이 있었다가) 비어 보이면, 패널이 리셋되는 중일
            # 수 있다는 신호로 기억해둔다 - 다시 채워지면 아래에서 새 핸드로 쓴다
            # (task.md "99%를 위한 안정화" 2절 "패널이 비워졌다가 다시 채워짐").
            # 단, 딱 한 번 비었다고 바로 인정하지 않는다 - 연속으로 일정 시간
            # 이상이어야(self.preflop_empty_confirm_streak, __post_init__ 참고)
            # "확정된 빈 상태"로 센다.
            if self._started and self._hand_preflop_keys:
                self._preflop_empty_streak += 1
        else:
            current_keys = {(_norm_name(r.player), r.action, r.amount) for r in preflop_rows}
            # 실측 확인(2026-08-21, 5개 영상 교차 검증): 폴링 주기(1.5초 안팎)가
            # NEW_HAND_CONFIRM_TARGET_SEC(0.4초)보다 길어서 __post_init__이 계산하는
            # new_hand_confirm_streak가 실제로는 1이 된다 - 즉 아래 겹침 검사(생략)가
            # "겹치는 게 하나도 없다"고 판단한 폴링이 단 1번만 있어도 곧바로 새 핸드로
            # 확정된다. 그런데 금액 OCR이 poll마다 흔들리는 경우(예: "400"이 다음
            # poll엔 "4예"로 깨져서 금액 없이 파싱됨 - _fix_ocr_amount_glyphs로 이번에
            # 상당수 고쳤지만 다른 종류의 금액 오독까지 전부 막아주진 못한다)
            # (이름,행동,금액) 튜플이 poll마다 달라지므로 이 겹침 검사가 "완전히 새
            # 패널"이라고 오판할 수 있다 - hand17→18, hand12→13에서 히어로 홀카드/
            # 액션 시퀀스가 그대로 반복되는 의심 사례로 확인된 것과 정황이 일치한다.
            # 그래서 겹침 판정 자체는 (이름,행동,금액) 완전일치 뿐 아니라 금액을 뺀
            # (이름,행동)만 겹쳐도 "이어지는 핸드"로 본다 - Fold는 원래 금액이 없어서
            # 이 완화로 전혀 영향받지 않고(기존 "폴드 재등장" 신호가 그대로 새 핸드를
            # 잡아준다), 금액이 있는 행동(콜/레이즈/올인 등)만 금액 오독 한 번으로
            # 핸드가 갈라지는 걸 막아준다.
            current_keys_no_amount = {(_norm_name(r.player), r.action) for r in preflop_rows}
            prev_keys_no_amount = {(n, a) for (n, a, _amt) in self._hand_preflop_keys}
            # 안티는 핸드 시작 때 딱 한 번, 다른 어떤 행동보다도 먼저 나온다는 시간
            # 순서 규칙 - 이번 핸드에서 안티가 아닌 행동(폴드/콜/레이즈 등)을 이미
            # 봤는데, 처음 보는 안티 줄이 또 보이면 이름이 매칭되든 안 되든 새
            # 핸드로 본다(이름 매칭에 기대지 않아서 OCR 이름 흔들림에도 안정적).
            new_ante_after_decision = self._hand_has_decisive_action and any(
                r.action == "Ante" and (_norm_name(r.player), r.action, r.amount) not in self._hand_preflop_keys
                for r in preflop_rows
            )
            # 폴드도 그 핸드 안에서는 되돌릴 수 없다 - 이미 폴드로 확정된 이름으로
            # 또 다른(처음 보는) 줄이 보이면 새 핸드로 본다. 프리플랍만 있는 짧은
            # 핸드가 연달아 나올 때(task.md "0→0 핸드"), 겹침 검사만으로는 우연히
            # 같은 폴드가 일치해서 새 핸드를 놓치는 경우가 있어 추가했다.
            fold_reentry = any(
                _norm_name(r.player) in self._hand_folded_names
                and (_norm_name(r.player), r.action, r.amount) not in self._hand_preflop_keys
                for r in preflop_rows
            )
            panel_refilled_after_empty = self._preflop_empty_streak >= self.preflop_empty_confirm_streak
            new_hand_signal = (
                new_ante_after_decision
                or fold_reentry
                or panel_refilled_after_empty
                or not (current_keys & self._hand_preflop_keys or current_keys_no_amount & prev_keys_no_amount)
            )
            # task.md "핸드 쪼개짐 수정": 신호 자체는 한 번의 폴링 스냅샷만 보고
            # 바로 확정하지 않는다 - 연속으로 new_hand_confirm_streak번 이상
            # 이어져야 진짜 새 핸드로 본다(preflop_empty_confirm_streak와 같은
            # 패턴). 신호가 중간에 끊기면(다음 폴링엔 다시 겹침 등) 스크롤 중
            # 순간적인 OCR 흔들림이었을 뿐이므로 스트릭만 리셋하고, 이번 폴링
            # 내용은 아래에서 평소처럼 현재 핸드에 그대로 합류시킨다. "이번이
            # 이 기록 세션의 첫 관측"(not self._started)은 기다릴 대상 자체가
            # 없으므로 디바운스 없이 바로 확정한다.
            if new_hand_signal:
                self._pending_new_hand_streak += 1
            else:
                self._pending_new_hand_streak = 0
            is_new_hand = (
                not self._started
                or self._pending_new_hand_streak >= self.new_hand_confirm_streak
            )
            if is_new_hand:
                self._pending_new_hand_streak = 0
                if self.syncing:
                    # task.md "기록 시작 동기화": is_new_hand는 위 감지 로직(건드리지
                    # 않음) 그대로 쓰되, 동기화 중엔 결과를 다르게 소비한다.
                    # was_started=False면 기록 시작 후 첫 관측일 뿐 - 지금 화면에
                    # 보이는 게 진행 중이던 핸드 조각인지 아닌지 아직 모르니, 베이스
                    # 라인만 잡고(고 조각은 버리고) 계속 기다린다. was_started=True로
                    # 이 분기에 다시 들어왔다는 건 그 베이스라인과 달라진 진짜 핸드
                    # 경계를 방금 잡았다는 뜻 - 그 순간부터 [1게임 시작]으로 시작한다.
                    was_started = self._started
                    self._started = True
                    self._reset_hand_state()
                    if was_started:
                        self.hand_no += 1
                        out.append(EmittedLine(
                            text=f"[{self.hand_no}게임 시작]", tag="hand_header", hand_no=self.hand_no,
                        ))
                        self.syncing = False
                else:
                    out.append(EmittedLine(text=self._begin_new_hand(hero_name), tag="hand_header", hand_no=self.hand_no))
                    self._started = True
            self._preflop_empty_streak = 0  # 내용이 다시 보였으니 스트릭은 항상 리셋
            # task.md "핸드 쪼개짐 수정": 신호가 떴지만(new_hand_signal) 아직
            # 확정은 안 된(is_new_hand=False) "판정 대기" 상태에서는 이번 폴링
            # 내용을 현재(구) 핸드의 누적 키셋에 합류시키지 않는다 - 합류시켜
            # 버리면 바로 다음 폴링이 "방금 합류된 내용과 겹친다"고 오판해서,
            # 진짜 새 핸드조차 디바운스가 요구하는 연속 신호를 절대 못 채우는
            # 부작용이 생긴다(실측 확인: overlap 기반 신호는 1폴링만 지나면
            # 항상 스스로 사라져버림). 신호가 없거나(계속 같은 핸드) 이미
            # 확정됐으면(방금 새 핸드로 리셋된 뒤, 그 핸드의 첫 내용으로) 평소
            # 대로 합류시킨다.
            if not new_hand_signal or is_new_hand:
                self._hand_preflop_keys |= current_keys
                if any(r.action != "Ante" for r in preflop_rows):
                    self._hand_has_decisive_action = True
                for r in preflop_rows:
                    if r.action == "Fold":
                        self._hand_folded_names.add(_norm_name(r.player))

        if self.syncing and self._sync_polls >= self.sync_timeout_polls:
            # task.md 4절 "실패 시": 동기화가 너무 오래 안 끝나면 포기하고 지금
            # 상태를 그대로 1게임으로 받아들여 로컬 모드로 진행한다(멈추지 않음).
            self._reset_hand_state()
            self._started = True
            self.hand_no += 1
            out.append(EmittedLine(text=f"[{self.hand_no}게임 시작]", tag="hand_header", hand_no=self.hand_no))
            self.syncing = False
            self.sync_timed_out = True

        if self.syncing:
            return out  # 동기화 중엔 일반 행동 로그를 출력하지 않는다

        if self._hero_folded_this_hand:
            return out  # task.md [E]: 히어로 폴드 후 이 핸드는 기록 중단

        # task.md "히어로 탈락 감지 및 기록 자동 중지": 히어로 닉네임과 매칭되는
        # 행이 이번 폴링에 하나라도 있으면(안티 포함 - 자리에 앉아있다는 증거로는
        # 충분함), 지금(이 폴링에서 새 핸드 판정이 이미 끝난 뒤) 확정된 핸드
        # 기준으로 "히어로가 보였다"로 표시해둔다. 위쪽(새 핸드 판정) 이후에 둬야
        # 한다 - 그렇지 않으면 새 핸드가 시작되는 바로 그 폴링에 히어로 행까지
        # 같이 보이는 경우, _begin_new_hand가 리셋하기 전에 세운 표시가 "이전"
        # 핸드 판정에 쓰이고 새로 시작되는 핸드엔 반영이 안 되는 순서 문제가 있었다.
        if hero_name and any(r.is_hero for rows in parsed.values() for r in rows):
            self._hero_seen_this_hand = True

        for street in self.layout.streets:
            street_rows = parsed.get(street) or []
            # task.md "스트리트 헤더 누락 수정": 헤더는 "이 컬럼에 뭔가 처음
            # 등장했는지"만 보고 찍는다 - 그 행을 실제로 내보내는 시점과 분리해둔다
            # (지금은 포지션을 못 읽어도 바로 내보내므로 사실상 같은 폴링에 같이
            # 찍히지만, 분리해두는 편이 안전하다).
            if street_rows and street not in self._street_started:
                out.append(EmittedLine(text=f"[{street}]", tag="street_header", hand_no=self.hand_no))
                self._street_started.add(street)

            for row in street_rows:
                key = self._row_key(row)
                if key in self._emitted_keys:
                    continue

                if row.action == "Ante":
                    # task.md "로그 안정화 4종" 1절: Ante는 로그/엑셀에 절대 안
                    # 남긴다. 포지션이 보이면 이번 핸드에 그 플레이어 포지션을
                    # 미리 배워두는 데만 쓰고(나중에 폴드/콜 등에서 "?"가 될 확률을
                    # 줄여줌), 그 외엔 그냥 건너뛴다.
                    if row.position != "?":
                        self._resolve_position(row)
                    # task.md "참가인원(추정) 표시": 안티를 낸 사람 이름을 모아둔다
                    # (포지션 인식 여부와 무관 - 이름만 읽혀도 참가 여부는 안다).
                    self._hand_ante_names.add(_norm_name(row.player))
                    self._emitted_keys.add(key)
                    continue

                # 포지션을 이번에 직접 읽었으면 그대로, 못 읽었으면 이번 핸드에 이미
                # 알고 있는 값으로 즉시 채워본다(포지션은 핸드 내내 고정 - task.md [A]).
                position = self._resolve_position(row)

                if position != "?":
                    # task.md "로그 안정화 4종" 2절: 이름이 폴링마다 OCR로 살짝
                    # 다르게 잡혀도(말줄임표 흔들림 등), 같은 스트리트에서 같은
                    # 포지션이 같은 행동을 같은 금액으로 또 하면 중복으로 본다 -
                    # 이름 기반 키만으로는 이런 중복을 못 걸렀던 게 실측 확인됐다.
                    pos_key = (street, position, row.action, row.amount)
                    if pos_key in self._emitted_position_keys:
                        self._emitted_keys.add(key)
                        continue
                    # 실측 확인: 금액 OCR만 poll마다 흔들려서(위 _last_emitted_ident_action
                    # 주석 참고) 같은 사람의 같은 행동이 금액만 다르게 또 찍히는 경우 -
                    # 바로 직전에 찍힌 행동과 (스트리트,포지션,행동)이 완전히 같으면
                    # (금액만 다름) 재발생으로 보고 버린다.
                    if self._last_emitted_ident_action == (street, position, row.action):
                        self._emitted_keys.add(key)
                        continue
                else:
                    # task.md "중복 기록 수정": 포지션을 이번에도 재사용으로도 못
                    # 채웠다는 건(_resolve_position 참고) 이 사람 포지션을 이
                    # 핸드에서 아직 한 번도 못 배웠다는 뜻이다 - 즉 위 포지션 기반
                    # 중복 방지가 걸릴 대상 자체가 없다. 실측 확인된 문제: 같은
                    # 행동이 이런 상태에서 이름만 다르게 읽혀 두세 번 찍힌 사례가
                    # 반복됐다(예: "걸bshed"/"bsheds" 같은 올인이 중복, "?" 포지션
                    # 그대로 "RAISE 6K"가 3번). 같은 (스트리트,행동,금액)으로 이미
                    # 포지션 미확정으로 낸 이름이 있고 이번 이름과 히어로 매칭에
                    # 쓰는 것과 같은 기준(접두어/편집거리 1)으로 같은 사람일 가능성이
                    # 높으면, OCR이 그 사이 이름을 다르게 읽은 같은 행동으로 보고
                    # 건너뛴다. 포지션이 이미 확정된 사람과는 절대 안 섞인다(이 분기
                    # 자체가 포지션 미확정일 때만 타므로) - 서로 다른 확정 포지션의
                    # 두 사람이 우연히 이름이 비슷한 경우는 위쪽 pos_key 검사가
                    # 이미 따로 처리한다.
                    name_norm = _norm_name(row.player)
                    action_key = (street, row.action, row.amount)
                    seen_names = self._hand_unresolved_action_names.setdefault(action_key, [])
                    if name_norm and any(_names_match(name_norm, seen) for seen in seen_names):
                        self._emitted_keys.add(key)
                        continue
                    # 실측 확인(video5, hand25): 포지션도 이름도 이번엔 아예 하나도 안
                    # 읽혀서(name_norm이 비었음) 위 이름 퍼지매칭 자체가 비교할 대상이
                    # 없는 경우 - "Jlexa9 All-in 49840"이 이미 찍힌 직후 이름/포지션이
                    # 전부 실패한 "? All-in 49840"이 또 찍히는 걸 실측 확인했다. 이름이
                    # 진짜 하나도 없고(포지션도 없음) 정확히 같은 (스트리트,행동,금액)이
                    # 이미 이번 핸드에 찍힌 적 있으면 그 재발생으로 본다 - 금액이 있는
                    # 행동에만 적용한다(Fold처럼 금액이 없는 행동까지 적용하면 서로 다른
                    # 두 명이 우연히 같은 스트리트에서 이름 없이 폴드하는 정상적인 경우를
                    # 잘못 합칠 위험이 있다).
                    if not name_norm and row.amount and seen_names:
                        self._emitted_keys.add(key)
                        continue
                    if name_norm:
                        seen_names.append(name_norm)

                # task.md "액션 순서 뒤섞임 수정": 포지션을 이번에도(재사용으로도)
                # 못 채웠다고 출력을 미루지 않는다 - 예전엔 몇 프레임 재시도
                # 대기열에 넣고 기다렸는데, 그 사이 다른 행이 먼저 나가면서 로그
                # 순서가 실제 딜러 채팅 순서와 달라졌다(실측 확인). 이제는 바로
                # 이름 접두어 폴백으로 이번 폴링에 출력한다(_format_action이 처리).
                row.position = position
                out.append(EmittedLine(
                    text=self._format_action(row, position),
                    tag="hero_action" if row.is_hero else "action",
                    row=row,
                    hand_no=self.hand_no,
                ))
                self._emitted_keys.add(key)
                if position != "?":
                    self._emitted_position_keys.add((street, position, row.action, row.amount))
                    self._last_emitted_ident_action = (street, position, row.action)
                else:
                    # 포지션 미확정인 행이 실제로 하나 나갔다는 건 그 사이 시간이
                    # 흘렀다는 뜻이므로, 위 "바로 직전 행동" 재발생 체크가 이후의
                    # 진짜 다른 사람 행동까지 잘못 묶지 않도록 갱신해둔다.
                    self._last_emitted_ident_action = None

                if row.is_hero and row.action == "Fold":
                    self._hero_folded_this_hand = True
                    return out  # 이후 줄은 이번 폴링에서도 더 보지 않는다

        return out
