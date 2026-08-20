"""OCR로 읽은 한 줄의 텍스트에서 홀덤 행동(Fold/Call/Raise/All-in 등)과
스트리트(프리플랍/플랍/턴/리버), 새 핸드 시작 여부를 판별한다.

특정 홀덤 프로그램의 문구에 완전히 맞추기는 어렵기 때문에,
여러 홀덤 프로그램에서 흔히 쓰이는 한글/영문 표현을 폭넓게 인식하도록 만들었다.
"""
import re
from dataclasses import dataclass

# (표시용 영문 행동명, 정규식) - 위에서부터 우선 검사한다.
# 실제 기록에 남기는 '핵심' 행동들 (플레이어가 직접 선택하는 행동)
ACTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("All-in", re.compile(r"(all\s*-?\s*in|올\s*인|맥스)", re.I)),
    # "fld"/"f0ld"는 실제 속도 테스트에서 확인된 "FOLD" OCR 오인식(짧게 스친 프레임에서
    # 'O'가 탈락하거나 '0'으로 오인식됨, 예: fLD/FLD/F0LD) - Fold 패턴 한 곳에서 같이 관리한다.
    ("Fold", re.compile(r"(fold|f[o0]?ld|폴드|폴더|다이|겹)", re.I)),
    ("Check", re.compile(r"(check|체크|첵)", re.I)),
    # "테이즈"는 코인포커 실측에서 확인된 "레이즈" OCR 오인식(4배 업스케일 적용 후에도
    # 첫 글자가 계속 틀림) - 유사 글자 보정으로 동의어 취급한다.
    ("Raise", re.compile(r"(raise|레이즈|레이스|따당|테이즈)", re.I)),
    ("Call", re.compile(r"(call|콜|호출)", re.I)),
    ("Bet", re.compile(r"(bet|벳|베팅|베트|쿼터|하프|내기)", re.I)),
]
LOGGED_ACTION_TYPES = {name for name, _ in ACTION_PATTERNS}

# 플레이어의 선택이 아니라 게임 상태를 나타내는 것들 (기록하지 않지만, 상태 갱신에 사용)
HAND_RESET_PATTERN = re.compile(
    r"(new\s*game|new\s*hand|새\s*게임|새\s*핸드|다음\s*게임|딜러\s*버튼)", re.I
)
STREET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("리버", re.compile(r"(river|리버)", re.I)),
    ("턴", re.compile(r"\bturn\b|턴", re.I)),
    ("플랍", re.compile(r"(flop|플랍)", re.I)),
]
BLIND_PATTERN = re.compile(
    r"(small\s*blind|big\s*blind|ante|스몰\s*블라인드|빅\s*블라인드|앤티|\bsb\b|\bbb\b)", re.I
)
WIN_PATTERN = re.compile(r"(win|winner|showdown|쇼다운|승리|이겼습니다|획득)", re.I)
OTHER_KEYWORD_PATTERNS = [BLIND_PATTERN, WIN_PATTERN]

DEFAULT_STREET = "프리플랍"

# (?<!\w)로 앞쪽 경계만 강제해서, "PLAYER99" 같은 이름/아이디에 섞인 숫자를 금액으로
# 오인하지 않게 한다. 뒤쪽은 \b를 쓰지 않는다 - "1000골드"처럼 숫자 뒤에 한글 단위가
# 공백 없이 바로 붙는 경우(\d와 한글 모두 \w라 그 사이엔 \b가 성립하지 않는다) 금액을
# 놓치기 때문이다(실측 OCR 결과로 확인).
# 코인포커는 팟/스택이 크면 "4.95K"(=4,950), "1.2M"(=1,200,000)처럼 천/백만 단위
# 축약 표기를 쓴다(실측 확인: 팟 표시 "4.89K" 등) - k/m 단위도 인식해서 실제 값으로
# 환산한다(안 그러면 "4.95K"의 K가 그냥 버려져서 4.95로 1000배 작게 기록됨).
AMOUNT_PATTERN = re.compile(r"(?<!\w)(\d[\d,]*(?:\.\d+)?)(?:\s*(원|골드|칩|chip|point|pt|k|m))?", re.I)

_UNIT_MULTIPLIER = {"k": 1000, "m": 1_000_000}

# "1만골드", "23만 4184골드"처럼 한글 만 단위로 표기된 금액. \d000 형태가 아니라서
# AMOUNT_PATTERN만으로는 못 읽는다(실측 로그에서 베트/콜 금액의 상당수가 이 형태였음).
KOREAN_MAN_PATTERN = re.compile(r"(?<!\w)(\d[\d,]*)\s*만(?:\s*(\d[\d,]*))?", re.I)

# 금액이 의미가 없는 행동 유형 (있어도 무시한다)
ACTIONS_WITHOUT_AMOUNT = {"Fold", "Check"}

# 한국어 문장에서 흔히 붙는 이름 뒤 조사들을 제거해서 플레이어 이름을 정리한다.
NAME_SUFFIXES = ["님께서", "님이", "님은", "님의", "님을", "님", "이", "가", "은", "는"]


@dataclass
class ParsedAction:
    kind: str  # "action" | "street" | "new_hand" | "result" | "other"
    action_type: str  # 예: "Fold" (kind == "action"일 때만 값 있음)
    player: str  # 추정된 플레이어 이름 (없으면 "")
    amount: str  # 추정된 금액 문자열 (없으면 "")
    raw_text: str  # OCR 원문
    street_name: str  # 예: "플랍" (kind == "street"일 때만 값 있음)

    @property
    def is_new_hand(self) -> bool:
        return self.kind == "new_hand"


def matches_any_keyword(text: str) -> bool:
    """텍스트가 행동/스트리트/새 핸드/기타 게임 키워드 중 하나라도 포함하는지."""
    if HAND_RESET_PATTERN.search(text):
        return True
    for _, pattern in STREET_PATTERNS:
        if pattern.search(text):
            return True
    for _, pattern in ACTION_PATTERNS:
        if pattern.search(text):
            return True
    for pattern in OTHER_KEYWORD_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _extract_player(text: str, action_span_start: int) -> str:
    prefix = text[:action_span_start].strip()
    if not prefix:
        return ""
    # 보통 "플레이어명(+조사) [금액] 행동 ..." 형태이므로, 맨 앞 토큰을 이름으로 간주한다.
    token = re.split(r"[\s:>\-]+", prefix)[0]
    for suf in NAME_SUFFIXES:
        if token.endswith(suf) and len(token) > len(suf):
            token = token[: -len(suf)]
            break
    return token.strip()


def _extract_amount(text: str) -> str:
    man_match = KOREAN_MAN_PATTERN.search(text)
    if man_match:
        man = int(man_match.group(1).replace(",", ""))
        rest = int(man_match.group(2).replace(",", "")) if man_match.group(2) else 0
        return str(man * 10000 + rest)

    matches = AMOUNT_PATTERN.findall(text)
    candidates = []  # (실제 값, 출력용 문자열)
    for number_str, unit in matches:
        digits = number_str.replace(",", "")
        if len(digits.replace(".", "")) < 2:
            continue
        multiplier = _UNIT_MULTIPLIER.get(unit.lower()) if unit else None
        value = float(digits) * multiplier if multiplier else float(digits)
        if multiplier:
            out = str(int(value)) if value == int(value) else str(value)
        else:
            out = number_str  # 단위 없으면 기존처럼 OCR 원문 그대로(콤마 포함) 유지
        candidates.append((value, out))
    if not candidates:
        return ""
    # 여러 숫자가 섞여 있으면(스택/베팅액 등) 실제 값이 가장 큰 것을 금액으로 추정한다.
    return max(candidates, key=lambda c: c[0])[1]


def parse_line(raw_text: str) -> ParsedAction:
    text = raw_text.strip()

    if HAND_RESET_PATTERN.search(text):
        return ParsedAction(kind="new_hand", action_type="", player="", amount="", raw_text=text, street_name="")

    for street_name, pattern in STREET_PATTERNS:
        if pattern.search(text):
            return ParsedAction(kind="street", action_type="", player="", amount="", raw_text=text, street_name=street_name)

    if BLIND_PATTERN.search(text):
        return ParsedAction(kind="other", action_type="", player="", amount="", raw_text=text, street_name="")

    m = WIN_PATTERN.search(text)
    if m:
        player = _extract_player(text, m.start())
        return ParsedAction(kind="result", action_type="", player=player, amount="", raw_text=text, street_name="")

    for action_name, pattern in ACTION_PATTERNS:
        m = pattern.search(text)
        if m:
            player = _extract_player(text, m.start())
            amount = "" if action_name in ACTIONS_WITHOUT_AMOUNT else _extract_amount(text)
            return ParsedAction(
                kind="action", action_type=action_name, player=player, amount=amount,
                raw_text=text, street_name="",
            )

    return ParsedAction(kind="other", action_type="", player="", amount="", raw_text=text, street_name="")


# 화면 우측 "내기록" 패널 한 줄 형식: "닉네임 : +52만 골드" / "닉네임 : -13만 골드".
# 콜론 바로 뒤에 +/-가 오는 것만 승패 결과로 보고, "일러비(딜러비) -1만8200골드"처럼
# 부호 앞에 다른 말이 낀 줄(수수료 등)은 걸러낸다.
RESULT_LINE_PATTERN = re.compile(r"^(.+?)\s*[:：]\s*([+\-])\s*(.+)$")


def parse_result_line(raw_text: str):
    """(닉네임, 부호, 금액문자열) 을 반환한다. 형식에 안 맞으면 None."""
    m = RESULT_LINE_PATTERN.match(raw_text.strip())
    if not m:
        return None
    nickname, sign, rest = m.group(1).strip(), m.group(2), m.group(3)
    amount = _extract_amount(rest)
    if not nickname or not amount:
        return None
    return nickname, sign, amount


# 코인포커: 별도 결과 패널이 없고, 이긴 좌석의 홀카드 위에 "+3,181"처럼 부호+금액만
# 잠깐 뜬다(실측 확인, 닉네임은 안 붙어 있음 - 그 좌석 이름표에 이미 나와 있어서).
# 부호 없이 순수 숫자만 있는 줄(스택 액수 등)과 헷갈리지 않게 "+"로 시작하는지만 본다.
WIN_AMOUNT_PATTERN = re.compile(r"^\+\s*(.+)$")


def parse_win_amount(raw_text: str) -> str:
    """"+3,181" 같은 줄에서 금액 문자열만 뽑는다. 형식에 안 맞으면 빈 문자열."""
    m = WIN_AMOUNT_PATTERN.match(raw_text.strip())
    if not m:
        return ""
    return _extract_amount(m.group(1))


# 베팅 칩 옆에 뜨는 "2,000" 같은 순수 금액 텍스트용 (부호도 행동 단어도 없음).
def extract_amount_from_text(text: str) -> str:
    return _extract_amount(text)
