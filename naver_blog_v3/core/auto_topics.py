"""Profile-bound automatic topic selection over collected metadata and evergreen ideas."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import atomic_write_json
from config import profile as _profile
from core import news_topics

_EVERGREEN: Dict[str, List[Tuple[str, str]]] = {
    "여행": [
        ("여행 일정 짤 때 이동 시간을 줄이는 순서", "목적지 수와 이동 시간을 함께 따져 일정을 어떻게 조정할까요?"),
        ("숙소 예약 전 위치와 교통을 비교하는 방법", "숙소 후보의 교통편과 이동 시간을 어떤 기준으로 비교할까요?"),
        ("여행 경비를 항목별로 나누어 계획하는 법", "교통·숙박·식비 예산을 어디서부터 나누어 잡을까요?"),
        ("비 오는 날 여행을 준비할 때 확인할 항목", "날씨가 달라졌을 때 일정과 준비물을 어떻게 조정할까요?"),
        ("아이와 떠나는 여행의 이동·휴식 간격 정하기", "아이의 연령과 이동 방식에 맞춰 휴식 간격을 어떻게 정할까요?"),
        ("혼자 여행할 때 숙소와 귀가 동선을 점검하는 법", "혼자 움직이는 일정에서 귀가 경로와 연락 계획을 어떻게 확인할까요?"),
        ("대중교통 여행에서 환승과 막차를 확인하는 순서", "환승 시간과 운행 종료 시각을 어떤 순서로 확인할까요?"),
        ("여행 짐을 일정과 체류 기간에 맞춰 줄이는 방법", "일정별로 꼭 필요한 짐과 현지에서 마련할 물품을 어떻게 나눌까요?"),
        ("숙소 취소·변경 조건을 예약 전에 확인하는 법", "예약 조건에서 취소 기한과 수수료를 어떻게 찾아볼까요?"),
        ("고령 가족과 여행할 때 이동 부담을 줄이는 계획", "걷는 거리와 계단, 휴식 장소를 어떻게 미리 확인할까요?"),
        ("여행 전 교통·숙소 예약 정보를 한곳에 정리하는 법", "예약 번호와 변경 연락처를 어떤 방식으로 정리하면 편할까요?"),
        ("짧은 주말 여행에서 일정 우선순위를 정하는 방법", "시간이 짧을 때 꼭 할 일과 다음으로 미룰 일을 어떻게 고를까요?"),
    ],
    "it": [
        ("스마트폰 사진과 파일을 안전하게 백업하는 방법", "기기 분실에 대비해 사진과 문서의 백업 여부를 어떻게 확인할까요?"),
        ("새 기기 설정 전에 확인할 보안 항목", "계정 복구 수단과 잠금 설정을 어떤 순서로 점검할까요?"),
        ("클라우드 저장 공간을 정리하는 실용적인 순서", "삭제 전에 필요한 파일의 백업 상태를 어떻게 확인할까요?"),
        ("앱 권한을 주기적으로 점검하는 방법", "앱이 실제 기능에 필요한 권한만 갖도록 어떻게 확인할까요?"),
        ("가족의 계정 복구 정보를 안전하게 정리하는 법", "복구 이메일과 전화번호가 현재도 사용할 수 있는지 어떻게 점검할까요?"),
        ("공용 Wi-Fi를 쓸 때 계정 정보를 지키는 기본 수칙", "민감한 계정 작업을 미루거나 보호할 방법은 무엇일까요?"),
        ("노트북 교체 전에 파일과 인증 수단을 옮기는 순서", "작업에 필요한 파일과 2단계 인증을 빠뜨리지 않으려면 무엇을 확인할까요?"),
        ("브라우저 비밀번호와 자동완성 정보를 점검하는 법", "공용 기기나 오래된 계정에 남은 자동완성 정보를 어떻게 정리할까요?"),
        ("소프트웨어 업데이트 전 백업과 호환성을 확인하는 방법", "업데이트 전 데이터 백업과 사용 중인 장치의 호환성을 어떻게 확인할까요?"),
        ("피싱 의심 메시지에서 링크를 확인하는 기본 절차", "보낸 사람과 주소를 확인하기 전에 어떤 행동을 피해야 할까요?"),
        ("휴대전화 저장 공간이 부족할 때 안전하게 정리하는 법", "삭제·이동 전에 사진과 앱 데이터의 백업 상태를 어떻게 확인할까요?"),
        ("업무용 파일의 버전과 공유 권한을 관리하는 방법", "최종본을 구분하고 필요한 사람에게만 공유하려면 어떻게 설정할까요?"),
        ("스마트 기기의 위치·마이크 권한을 확인하는 순서", "항상 허용과 사용 중 허용을 어떤 기준으로 나눌까요?"),
        ("집에서 사용하는 공유기의 기본 보안 설정 점검", "관리자 비밀번호와 연결 기기 목록을 어디서 확인할까요?"),
        ("사진 파일 이름과 폴더를 찾기 쉽게 정리하는 방법", "날짜와 프로젝트 기준 중 어떤 구성이 검색에 편할까요?"),
        ("새 앱을 설치하기 전에 개인정보 안내를 살펴보는 법", "수집 항목과 보관 기간에서 어떤 부분을 확인해야 할까요?"),
    ],
    "건강": [
        ("건강검진 결과를 기록하고 다음 검진을 준비하는 방법", "결과지에서 다음 검진 시기와 의료진에게 물을 내용을 어떻게 정리할까요?"),
        ("운동 습관을 무리 없이 일정에 넣는 법", "현재 생활 리듬에 맞춰 실천 가능한 운동 시간을 어떻게 정할까요?"),
        ("복용 중인 약과 진료 일정을 한곳에 기록하는 방법", "진료 때 정확한 정보를 전달하려면 어떤 항목을 적어 둘까요?"),
        ("집에서 혈압을 기록할 때 측정 조건을 일정하게 하는 법", "측정 시간과 자세를 어떻게 일정하게 유지하고 의료진에게 전달할까요?"),
        ("오래 앉아 일할 때 휴식 알림과 자세를 점검하는 방법", "업무 흐름을 방해하지 않으면서 몸을 움직일 시간을 어떻게 정할까요?"),
        ("수면 시간을 기록해 생활 패턴을 살펴보는 법", "수면 시간과 기상 시간을 어떻게 기록하면 변화를 보기 쉬울까요?"),
        ("진료 전에 증상과 질문을 간단히 정리하는 방법", "증상 시작 시점과 변화, 궁금한 점을 어떤 순서로 적을까요?"),
        ("가족의 응급 연락처와 복용 정보 정리하기", "필요할 때 가족이 최신 연락처와 복용 정보를 찾도록 어디에 둘까요?"),
        ("건강 정보를 온라인에서 읽을 때 출처를 확인하는 법", "작성 주체와 갱신일, 근거를 어떤 순서로 확인할까요?"),
        ("걷기 목표를 현재 활동량에 맞춰 조정하는 방법", "처음부터 무리하지 않도록 목표를 어떻게 나누어 설정할까요?"),
    ],
    "요리": [
        ("일주일 식단을 세우고 식재료 낭비를 줄이는 방법", "이미 있는 재료와 식사 일정을 어떻게 먼저 확인할까요?"),
        ("남은 식재료를 안전하게 보관하는 기본 원칙", "보관 용기와 날짜 표시를 어떻게 정리하면 놓치지 않을까요?"),
        ("장보기 전에 냉장고 재고를 빠르게 확인하는 법", "먼저 써야 할 재료를 식단에 어떻게 반영할까요?"),
        ("처음 요리할 때 조리 순서를 나누어 준비하는 방법", "손질·가열·정리 과정을 어떤 순서로 배치하면 편할까요?"),
        ("식품 라벨에서 보관법과 알레르기 정보를 확인하는 법", "구매 전에 꼭 확인할 성분과 보관 조건은 무엇일까요?"),
        ("간단한 도시락을 준비할 때 보관 시간을 고려하는 법", "출발 시각과 보관 환경에 맞춰 메뉴를 어떻게 고를까요?"),
        ("조리 도구를 용도별로 정리해 준비 시간을 줄이는 법", "자주 쓰는 도구와 안전한 보관 위치를 어떻게 정할까요?"),
        ("가족 식사에서 각자 선호와 제한 식품을 기록하는 방법", "재료 선택 전에 알레르기와 식사 선호를 어떻게 확인할까요?"),
        ("한 가지 재료를 여러 끼니에 나누어 활용하는 계획", "상하기 전에 사용할 분량과 조리법을 어떻게 나눌까요?"),
        ("조리 후 주방을 빠르게 정돈하는 작은 습관", "다음 요리를 편하게 시작하려면 어떤 정리를 먼저 할까요?"),
    ],
    "재테크": [
        ("월 지출을 고정비와 변동비로 나누어 점검하는 방법", "최근 지출에서 매달 반복되는 항목을 어떻게 구분할까요?"),
        ("가계 예산을 현실적으로 세우는 순서", "필수 지출과 저축 목표를 어떤 순서로 반영할까요?"),
        ("구독 서비스를 정리할 때 결제일과 해지 조건 확인", "사용 여부와 다음 결제일을 어떻게 한눈에 비교할까요?"),
        ("비상자금 목표와 월 적립액을 함께 정하는 방법", "현재 꼭 필요한 지출을 고려해 적립 계획을 어떻게 세울까요?"),
        ("카드 명세서에서 반복 결제와 낯선 항목을 찾는 법", "결제 내역의 상호와 날짜를 어떻게 대조하면 좋을까요?"),
        ("목돈 지출 전에 비교 기준과 예산 상한 정하기", "가격 외에 유지 비용과 조건을 어떤 기준으로 비교할까요?"),
        ("가족 공동 지출을 기록하고 정산하는 간단한 규칙", "누가 언제 어떤 항목을 기록하고 확인할지 어떻게 정할까요?"),
        ("금융 상품 안내에서 수수료와 적용 조건을 읽는 법", "가입 전에 비용과 조건을 어디서 확인해야 할까요?"),
        ("월말에 예산과 실제 지출 차이를 정리하는 방법", "차이가 생긴 항목을 다음 달 계획에 어떻게 반영할까요?"),
        ("온라인 금융 정보의 작성 주체와 날짜를 확인하는 법", "광고와 설명 자료를 구분할 때 어떤 표시를 살펴볼까요?"),
    ],
    "육아": [
        ("아이와 외출할 때 준비물을 상황별로 나누는 방법", "외출 시간과 장소에 따라 꼭 필요한 물품을 어떻게 고를까요?"),
        ("가정에서 하루 일과를 편하게 정리하는 법", "아이의 일정과 보호자의 일정을 어디에 함께 표시할까요?"),
        ("아이 병원 방문 전에 증상과 질문을 기록하는 방법", "진료에 필요한 변화와 궁금한 점을 어떻게 간단히 정리할까요?"),
        ("등하원 연락처와 귀가 계획을 공유하는 기본 규칙", "보호자 간 변경 사항을 빠뜨리지 않게 어떻게 공유할까요?"),
        ("장난감과 학습 도구를 사용 장소별로 정리하는 법", "아이가 스스로 찾고 정리하기 쉽게 위치를 어떻게 정할까요?"),
        ("가족 외출에서 휴식과 간식 시간을 계획하는 방법", "이동 거리와 아이의 컨디션에 맞춰 쉬는 시간을 어떻게 둘까요?"),
        ("아이 옷과 계절 용품을 다음 사용 시기별로 보관하기", "크기와 계절을 표시해 필요한 것을 쉽게 찾으려면 어떻게 할까요?"),
        ("가정 내 디지털 기기 사용 규칙을 함께 정하는 순서", "가족이 함께 지킬 시간과 장소 기준을 어떻게 정할까요?"),
        ("아이의 준비물을 스스로 확인하도록 돕는 체크리스트", "연령과 일정에 맞춰 체크 항목을 어떻게 단순하게 만들까요?"),
        ("돌봄 인계 때 일정과 주의 사항을 전달하는 방법", "중요한 일정과 연락 방법을 어떤 형식으로 공유하면 좋을까요?"),
    ],
    "생활": [
        ("집안의 반복 업무를 주간 단위로 정리하는 방법", "꼭 해야 할 일과 주기를 어디에 기록하면 놓치지 않을까요?"),
        ("중요 서류를 찾기 쉽게 보관하는 기본 규칙", "문서 종류와 보관 기한에 따라 어떻게 분류하면 좋을까요?"),
        ("계절이 바뀔 때 집안 점검 항목을 정리하는 법", "공간별로 어떤 점검을 할지 간단한 목록으로 어떻게 만들까요?"),
        ("가족이 함께 쓰는 일정표를 간단히 운영하는 방법", "누구나 일정을 추가하고 변경을 알아보도록 어떻게 정할까요?"),
        ("자주 쓰는 물건의 자리를 정해 찾는 시간을 줄이는 법", "사용 장소와 빈도를 기준으로 보관 위치를 어떻게 정할까요?"),
        ("가전제품 설명서와 보증 정보를 한곳에 모으는 방법", "모델명과 구매일, 수리 연락처를 어떻게 기록할까요?"),
        ("집을 비우기 전에 전기·가스·창문을 확인하는 순서", "외출 시간이 길 때 확인할 항목을 어떻게 체크할까요?"),
        ("가정의 비상 연락처와 대피 정보를 공유하는 법", "가족이 쉽게 찾을 수 있는 위치에 어떤 정보를 둘까요?"),
        ("청소 도구를 공간과 용도에 맞게 나누어 보관하기", "필요한 장소 가까이에 안전하게 두려면 어떻게 배치할까요?"),
        ("택배와 우편물을 날짜별로 정리해 놓치지 않는 법", "확인·보관·처리할 항목을 어떤 순서로 나누면 편할까요?"),
    ],
}
_CUSTOM_ANGLES = [
    ("정보를 확인할 때 출처와 갱신 날짜를 살펴보는 방법", "작성 주체와 갱신일을 어디에서 확인할 수 있을까요?"),
    ("처음 시작하기 전에 준비 항목과 순서를 정리하는 법", "현재 조건에서 먼저 준비할 항목은 무엇일까요?"),
    ("여러 선택지를 예산과 조건별로 비교하는 방법", "가격 외에 어떤 조건을 나란히 비교해야 할까요?"),
    ("초보자가 자주 놓치는 설정과 점검 순서", "시작 전에 놓치기 쉬운 확인 항목은 무엇일까요?"),
    ("가족과 함께 준비할 때 역할을 나누는 방법", "누가 무엇을 준비하고 확인할지 어떻게 정할까요?"),
    ("필요한 기록을 한곳에 모아 다시 찾기 쉽게 만드는 법", "다음에 참고할 정보와 날짜를 어떻게 기록하면 편할까요?"),
    ("오래 쓰기 전 유지 비용과 관리 주기를 확인하는 방법", "처음 비용 외에 어떤 유지 항목을 함께 봐야 할까요?"),
    ("문제가 생겼을 때 확인할 연락처와 대응 순서 정하기", "도움이 필요할 때 공식 연락처와 절차를 어디서 찾을까요?"),
    ("시간이 부족할 때 핵심 준비를 우선순위로 나누는 법", "지금 해야 할 일과 나중에 할 일을 어떻게 구분할까요?"),
    ("경험이 다른 사람에게 설명할 때 핵심 용어를 정리하는 방법", "처음 듣는 사람도 이해하도록 어떤 표현부터 풀어쓸까요?"),
]
_ALIASES = {"여행": ("여행", "관광", "숙소", "항공", "여행지", "축제", "festival", "travel"),
            "it": ("it", "기술", "소프트웨어", "앱", "스마트폰", "인공지능", "개발"),
            "건강": ("건강", "운동", "의료", "병원"), "요리": ("요리", "음식", "레시피"),
            "재테크": ("재테크", "금융", "투자", "경제", "절약"),
            "육아": ("육아", "아이", "어린이", "교육"), "생활": ("생활", "정리", "살림")}
log = logging.getLogger(__name__)
_RUNTIME_BLOCKED: Dict[str, set] = {}
_AUDIENCES = ("처음 준비하는 사람", "시간이 부족한 직장인", "가족과 함께하는 사람",
              "예산을 아끼려는 사람", "혼자 결정해야 하는 사람", "정보를 비교 중인 초보자")
_SITUATIONS = ("시작 전", "선택지를 비교할 때", "일정이 바뀌었을 때",
               "문제가 생겼을 때", "기록을 정리할 때", "다음 계획을 세울 때")


def _profile_name(profile: Optional[str]) -> str:
    name = profile or _profile.active_profile()
    if not _profile.is_valid_name(name):
        raise ValueError("프로필 이름이 올바르지 않습니다")
    return name


def _history_path(profile: Optional[str]) -> Any:
    return _profile.app_data_root(_profile_name(profile)) / "config" / "auto_topic_history.json"


def _tokens(value: str) -> set:
    return {token.casefold() for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", value or "")}


def _matches_alias(value: str, alias: str) -> bool:
    return alias.casefold() in _tokens(value) if len(alias) <= 2 else alias.casefold() in value.casefold()


def _topics(preferences: Dict[str, Any], editorial: Optional[Dict[str, Any]]) -> List[str]:
    explicit = [str(value).strip() for value in preferences.get("interests", []) if str(value).strip()]
    if explicit:
        return explicit
    editorial = editorial if isinstance(editorial, dict) else {}
    mode = editorial.get("topic_mode") or editorial.get("TOPIC_MODE")
    genre = editorial.get("brand_genre") or editorial.get("genre") or editorial.get("BLOG_GENRE")
    excluded = {"general", "auto", "자동", "일반"}
    result = [mode.strip()] if isinstance(mode, str) and mode.strip() and mode.strip().casefold() not in excluded else []
    if isinstance(genre, str) and genre.strip() and genre.strip().casefold() not in excluded:
        family = _family([genre])
        if family != "생활" or any(_matches_alias(genre, alias) for alias in _ALIASES["생활"]):
            result.append(genre.strip())
    return result


def _family(topics: List[str]) -> str:
    value = " ".join(topics).casefold()
    for family, aliases in _ALIASES.items():
        if any(_matches_alias(value, alias) for alias in aliases):
            return family
    return "생활"


def _file_exists_readonly(path: Any) -> Optional[bool]:
    """Distinguish a missing history file from a path whose state cannot be read."""
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("자동 주제 이력 경로 상태를 확인할 수 없습니다(%s): %s",
                    path.name, type(exc).__name__)
        return None


def _history_read_error(message: str) -> Dict[str, Any]:
    log.warning("%s 자동 선정을 차단했습니다. 원본과 백업을 보존하고 이력 파일을 확인해 주세요.", message)
    return {"_read_error": True}


def _history(profile: Optional[str]) -> Dict[str, Any]:
    """Read duplicate history without repairing or backing up profile files."""
    path = _history_path(profile)
    primary_exists = _file_exists_readonly(path)
    if primary_exists is None:
        return _history_read_error("자동 주제 이력 파일 상태를 알 수 없어")
    if not primary_exists:
        backup = path.with_suffix(".bak")
        backup_exists = _file_exists_readonly(backup)
        if backup_exists is None:
            return _history_read_error("자동 주제 백업 파일 상태를 알 수 없어")
        if backup_exists:
            return _history_read_error("주 이력이 없고 백업만 남아 있어")
    missing = {"_missing_history": True}
    value = news_topics._read_json_readonly(path, default=lambda: missing)
    if value is news_topics.READ_ERROR:
        return _history_read_error("자동 주제 이력을 읽지 못해")
    if isinstance(value, dict) and value.pop("_missing_history", False):
        # A concurrent disappearance is still safe only if neither file exists.
        primary_now = _file_exists_readonly(path)
        backup_now = _file_exists_readonly(path.with_suffix(".bak"))
        if primary_exists is False and primary_now is False and backup_now is False:
            return {}
        return _history_read_error("자동 주제 이력 상태가 읽는 도중 바뀌어")
    required = ("source_ids", "pending_ids", "evergreen_titles")
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(key), list) for key in required)
            or any(not isinstance(item, str) for key in required for item in value[key])):
        return _history_read_error("자동 주제 이력 형식이 손상되어")
    return value


def _identity(card: Dict[str, Any]) -> str:
    url = str(card.get("url") or "").rstrip("/").casefold()
    title = str(card.get("title") or "").casefold().strip()
    material = url or title
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_relevant(card: Dict[str, Any], topics: List[str]) -> bool:
    haystack = " ".join(str(card.get(key) or "") for key in ("title", "keywords", "source_name"))
    haystack_tokens = _tokens(haystack)
    source = str(card.get("source_name") or "").casefold()
    kind = str(card.get("kind") or "").casefold()
    for topic in topics:
        topic_tokens = _tokens(topic)
        if ((len(topic) > 2 and topic.casefold() in haystack.casefold())
                or (topic_tokens and topic_tokens.intersection(haystack_tokens))):
            return True
        family = _family([topic])
        if family != "생활" and any(_matches_alias(haystack, alias) for alias in _ALIASES[family]):
            return True
        if family == "여행" and kind == "event" and source in {"한국관광공사 tourapi", "서울 문화행사"}:
            return True
        if family == "it" and any(term in haystack.casefold() for term in
                                   ("ai", "인공지능", "개발자", "소프트웨어", "보안", "제품", "업데이트")):
            return True
    return False


def _valid_fresh_card(card: Dict[str, Any], now: datetime) -> bool:
    if (card.get("stale") or card.get("refresh_failed") or card.get("stale_reason")
            or card.get("verified") is True):
        return False
    source = str(card.get("source_name") or "").strip()
    url = str(card.get("url") or "").strip()
    provider_time = news_topics._date_value(card.get("provider_time"))
    if not source or not url or provider_time is None:
        return False
    if provider_time.astimezone(timezone.utc) > now.astimezone(timezone.utc):
        return False
    if (now.astimezone(timezone.utc) - provider_time.astimezone(timezone.utc)).total_seconds() > 7 * 86400:
        return False
    end = news_topics._date_value(card.get("event_end"))
    return not end or end.astimezone(news_topics.KST).date() >= now.astimezone(news_topics.KST).date()


def _news_candidate(card: Dict[str, Any], topics: List[str]) -> Dict[str, Any]:
    title = str(card.get("title") or "").strip()
    context = {key: card.get(key) for key in ("source_name", "url", "provider_time", "event_start",
                "event_end", "effective_date", "sources", "reader_question")}
    reason = f"설정 주제({', '.join(topics)})와 제목이 일치하고 날짜가 확인된 자료입니다."
    original_why = str(card.get("why_now") or "").strip()
    context.update({"basis": "latest_source", "selected_because": reason,
                    "why_now": f"{reason} {original_why}".strip(), "kind": card.get("kind", "news"),
                    "verified": False, "original_title": title})
    return {"title": title, "source": card.get("source_name"), "_auto_topic": True,
            "_topic_identity": _identity(card), "topic_context": context}


def _evergreen_candidates(topics: List[str], profile: Optional[str], count: int) -> List[Dict[str, Any]]:
    family = _family(topics)
    if family == "생활" and not any(any(_matches_alias(" ".join(topics), alias)
                                      for alias in aliases) for aliases in _ALIASES.values()):
        items = [(f"{topics[0]} {title}", f"{topics[0]}와 관련해 {question}")
                 for title, question in _CUSTOM_ANGLES]
    else:
        items = _EVERGREEN[family]
    history = _history(profile)
    if history.get("_read_error"):
        return []
    used = set(history.get("evergreen_titles", [])) if isinstance(history.get("evergreen_titles"), list) else set()
    blocked = set(history.get("source_ids", [])) | set(history.get("pending_ids", []))
    blocked.update(_RUNTIME_BLOCKED.get(_profile_name(profile), set()))
    candidates = []
    topic_label = family if family != "생활" else topics[0]
    reason = f"최신 관련 자료가 부족해 {topic_label} 주제의 실용 상시 각도로 골랐습니다."
    for title, question in items:
        for audience in _AUDIENCES:
            for situation in _SITUATIONS:
                angled_title = f"{title}: {audience}가 {situation} 확인할 순서"
                identity = hashlib.sha256(angled_title.casefold().encode("utf-8")).hexdigest()
                if angled_title in used or identity in blocked:
                    continue
                candidates.append({"title": angled_title, "source": "evergreen", "_auto_topic": True,
                    "_topic_identity": identity,
                    "topic_context": {"basis": "evergreen", "topics": topics,
                        "kind": "evergreen", "selected_because": reason,
                        "why_now": f"{reason} 최신 뉴스나 사용 경험을 뜻하지 않습니다.",
                        "source_name": None, "url": None, "provider_time": None,
                        "reader_question": f"{audience}가 {situation} {question}", "verified": False}})
                if len(candidates) >= count:
                    return candidates
    if len(candidates) < count:
        log.warning("분야별 상시 글감이 소진되었습니다. 새 관련 자료를 켜거나 관심 주제를 바꾸어 주세요.")
    return candidates


def select_candidates(profile: Optional[str] = None, *, editorial: Optional[Dict[str, Any]] = None,
                      refresh: bool = True, limit: int = 1, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Select related fresh metadata, then varied practical evergreen topics when needed."""
    profile = _profile_name(profile)
    try:
        count = max(1, min(int(limit), 120))
    except (TypeError, ValueError, OverflowError):
        count = 1
    preferences = news_topics.load_preferences(profile, read_only=True)
    if not preferences.get("auto_enabled"):
        return []
    topics = _topics(preferences, editorial)
    if not topics:
        return []
    history = _history(profile)
    if history.get("_read_error"):
        log.warning("자동 주제 이력을 읽을 수 없어 중복 안전을 위해 선정을 중단했습니다. 프로필 이력 파일을 복구해 주세요.")
        return []
    effective = dict(preferences)
    if not effective.get("interests"):
        effective["interests"] = topics[:30]
    has_enabled_source = any(effective.get(key) for key in
                             ("naver_enabled", "tour_enabled", "seoul_enabled")) or any(
                                 feed.get("enabled") for feed in effective.get("feeds", []))
    if refresh and has_enabled_source:
        news_topics.collect_topics(effective, profile)
    snapshot = news_topics.load_snapshot(profile, effective_preferences=effective, read_only=True)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=news_topics.KST)
    used_ids = set(history.get("source_ids", [])) if isinstance(history.get("source_ids"), list) else set()
    used_ids.update(history.get("pending_ids", []))
    used_ids.update(_RUNTIME_BLOCKED.get(profile, set()))
    cards = [card for card in snapshot.get("items", []) if isinstance(card, dict)
             and _valid_fresh_card(card, instant) and _is_relevant(card, topics)
             and _identity(card) not in used_ids]
    cards.sort(key=lambda card: news_topics._date_value(card.get("provider_time")) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    selected = [_news_candidate(card, topics) for card in cards[:count]]
    if len(selected) < count:
        selected.extend(_evergreen_candidates(topics, profile, count - len(selected)))
    return selected


def is_auto_enabled(profile: Optional[str] = None) -> bool:
    """Return the profile's explicit automatic-selection switch without side effects."""
    return bool(news_topics.load_preferences(_profile_name(profile), read_only=True).get("auto_enabled"))


def is_auto_configured(profile: Optional[str] = None,
                        editorial: Optional[Dict[str, Any]] = None) -> bool:
    """Enable the automatic queue only when the switch and a usable topic both exist."""
    name = _profile_name(profile)
    preferences = news_topics.load_preferences(name, read_only=True)
    return bool(preferences.get("auto_enabled") and _topics(preferences, editorial))


def record_success(candidate: Dict[str, Any], profile: Optional[str] = None) -> None:
    """Record only completed drafts or confirmed successful publishes for duplicate control."""
    if not isinstance(candidate, dict) or not candidate.get("_auto_topic"):
        return
    name = _profile_name(profile)
    identity = candidate.get("_topic_identity")
    if identity:
        _RUNTIME_BLOCKED.setdefault(name, set()).add(str(identity))
    history = _history(name)
    if history.get("_read_error"):
        log.error("자동 주제 이력이 손상되어 성공 이력을 기록하지 못했습니다. 중복 방지를 위해 자동 선정을 차단합니다.")
        return
    ids = list(history.get("source_ids", [])) if isinstance(history.get("source_ids"), list) else []
    if identity and identity not in ids:
        ids.append(identity)
    prior_pending = history.get("pending_ids", [])
    pending = [value for value in prior_pending if value != identity] if isinstance(prior_pending, list) else []
    if identity:
        write_ahead = list(prior_pending) if isinstance(prior_pending, list) else []
        if identity not in write_ahead:
            write_ahead.append(identity)
        try:
            atomic_write_json(_history_path(name), {
                "source_ids": ids, "pending_ids": write_ahead,
                "evergreen_titles": list(dict.fromkeys(history.get("evergreen_titles", []))),
                "updated_at": datetime.now(timezone.utc).isoformat()})
        except (OSError, ValueError, TypeError):
            log.error("자동 주제 성공 전 중복 차단 이력 저장 실패", exc_info=True)
            return
    evergreen = list(history.get("evergreen_titles", [])) if isinstance(history.get("evergreen_titles"), list) else []
    if candidate.get("topic_context", {}).get("basis") == "evergreen":
        evergreen.append(candidate.get("title", ""))
    context = candidate.get("topic_context") if isinstance(candidate.get("topic_context"), dict) else {}
    payload = {"source_ids": ids, "pending_ids": pending,
            "evergreen_titles": list(dict.fromkeys(evergreen)), "last_selection": {
                "title": candidate.get("title"), "basis": context.get("basis"),
                "selected_because": context.get("selected_because"),
                "source_name": context.get("source_name"), "provider_time": context.get("provider_time")},
            "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        atomic_write_json(_history_path(name), payload)
        if identity:
            _RUNTIME_BLOCKED.setdefault(name, set()).discard(str(identity))
    except (OSError, ValueError, TypeError):
        log.error("자동 주제 성공 이력 저장 실패", exc_info=True)


def record_pending(candidate: Dict[str, Any], profile: Optional[str] = None) -> bool:
    """Write-ahead block a source once submission may begin, including uncertain outcomes."""
    if not isinstance(candidate, dict) or not candidate.get("_auto_topic"):
        return False
    name = _profile_name(profile)
    identity = candidate.get("_topic_identity")
    if identity:
        _RUNTIME_BLOCKED.setdefault(name, set()).add(str(identity))
    history = _history(name)
    if history.get("_read_error"):
        log.error("자동 주제 이력이 손상되어 미확정 이력을 기록하지 못했습니다. 자동 선정을 차단합니다.")
        return False
    existing_pending = history.get("pending_ids", [])
    pending = list(existing_pending) if isinstance(existing_pending, list) else []
    if identity and identity not in pending:
        pending.append(identity)
    history.setdefault("source_ids", [])
    history.setdefault("evergreen_titles", [])
    history["pending_ids"] = pending
    history["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write_json(_history_path(name), history)
        return True
    except (OSError, ValueError, TypeError):
        log.error("자동 주제 미확정 이력 저장 실패", exc_info=True)
        return False


def last_selection(profile: Optional[str] = None) -> Dict[str, Any]:
    """Return the last successful automatic topic for optional UI review."""
    value = _history(profile).get("last_selection")
    return dict(value) if isinstance(value, dict) else {}
