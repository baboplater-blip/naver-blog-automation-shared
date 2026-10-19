# config/profile_doctor.py — 프로필 발행 준비 점검(새 블로그가 오류 없이 돌게)
"""새 블로그 프로필이 '오류 없이 운영 가능한지'를 사전 점검한다.

exampleprofile 추가 때 겪은 오류들(DataLab 401·정부 브랜딩 누수·마감 주입 등)을 **실행 전에**
잡기 위한 프리플라이트. 자격증명 값은 절대 노출하지 않는다(존재/마스킹만).

사용:
    from config import profile_doctor
    print(profile_doctor.report("exampleprofile"))
    r = profile_doctor.diagnose("exampleprofile")   # {ok, checks:[{key,status,msg}], errors, warns}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from config import profile as _pf

_PLACEHOLDERS = ("your_client_id", "your_client_secret", "your_naver_id")


def _key_set_in(cfg_path: Path, key: str) -> bool:
    """cfg_path(user_settings.json)에 key 가 실값(placeholder 아님)으로 있는가. 값 비노출."""
    try:
        if not cfg_path.exists():
            return False
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        v = raw.get(key)
        return bool(v) and v not in _PLACEHOLDERS
    except (OSError, ValueError):
        return False


def diagnose(name: Optional[str] = None) -> dict:
    """프로필 발행 준비 점검. 반환 {profile, checks, ok, errors, warns}.
    status: ok(정상) | warn(동작하나 권장사항) | error(발행 불가)."""
    name = name or _pf.active_profile()
    is_def = _pf.is_default(name)
    checks: list[dict] = []

    def add(key: str, status: str, msg: str) -> None:
        checks.append({"key": key, "status": status, "msg": msg})

    # 1) 자격증명(네이버 계정) — 필수·격리
    try:
        d = _pf.describe(name)
        if d.get("has_credentials") and d.get("naver_id_masked"):
            add("credentials", "ok", f"네이버 계정 설정됨 ({d['naver_id_masked']})")
        else:
            add("credentials", "error", "네이버 아이디/비밀번호 미설정 — 로그인·발행 불가")
    except Exception as e:  # noqa: BLE001
        add("credentials", "error", f"자격증명 확인 실패: {e}")

    # 2) DataLab/검색 API 앱키 — 자체 또는 default 상속(공유 폴백)
    own = _key_set_in(_pf.credentials_path(name), "api_client_id")
    inherited = _key_set_in(_pf.credentials_path("default"), "api_client_id")
    if own:
        add("api_keys", "ok", "트렌드/검색 API 키 사용 가능 (이 프로필 자체 키)")
    elif inherited:
        add("api_keys", "ok", "트렌드/검색 API 키 사용 가능 (default 프로필 키 자동 상속)")
    else:
        add("api_keys", "warn",
            "DataLab 앱키 없음 — 트렌드/검색 스킵됨. "
            "default 프로필 설정에 API 키를 넣으면 모든 프로필이 자동 상속")

    # 3) 브랜드 팩(정체성)
    if is_def:
        add("brand", "ok", "default(내장 examplebrand 기본)")
    else:
        from config import brand
        bd = _pf.brand_dir(name)
        bj = (bd / "brand.json") if bd is not None else None
        if bj is not None and bj.exists():
            add("brand", "ok", f"브랜드 팩 있음 (표시명: {brand.display(name)})")
        else:
            add("brand", "warn",
                "brand.json 없음 — 중립 기본값으로 안전 동작(정부 문구 누수 없음, 단 미브랜딩)")
        raw = brand._brand_json(name)   # noqa: SLF001 — 필드 채움 정도만 점검(값 비민감)
        miss = [k for k in ("writer_role", "primary_sources", "reliability_block", "blog_name")
                if not raw.get(k)]
        if miss:
            add("brand_fields", "warn",
                f"생성 프롬프트 도메인 필드 미설정: {', '.join(miss)} "
                f"(중립값으로 동작 — 채우면 이 분야에 특화됨)")

    # 4) 키워드 엑셀(발행 후보·트렌드 소스)
    from config import brand
    xls = brand.excel_path(name)
    if xls and Path(xls).exists():
        add("keywords", "ok", "키워드 엑셀 있음")
    else:
        loc = "profiles/<name>/keywords/*.xlsx" if not is_def else "앱 루트 *.xlsx"
        add("keywords", "warn", f"키워드 엑셀 없음 — 발행 후보/트렌드 비어있음 ({loc} 배치)")

    # 5) 카테고리·마감(named 전용 안전 점검)
    if not is_def:
        from config import brand
        cats = brand.categories(name)
        if cats:
            add("categories", "ok", f"카테고리 {len(cats)}종 인식")
        else:
            add("categories", "warn",
                "카테고리 미설정 — 전부 default 처리(brand.json categories 또는 "
                "brand/templates/prompt_strategies.json 권장)")
        if brand.deadline_enabled(name):
            add("deadline", "warn",
                "마감 추적 ON — 정부 공고가 발행 큐에 주입됨. "
                "비정부 블로그면 brand.json 의 deadline_tracker 를 빼세요")

    errors = [c for c in checks if c["status"] == "error"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {"profile": name, "checks": checks, "ok": not errors,
            "errors": errors, "warns": warns}


def report(name: Optional[str] = None) -> str:
    """사람이 읽는 점검 리포트(한글)."""
    r = diagnose(name)
    icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}
    head = "발행 준비 완료" if r["ok"] else "필수 항목 누락(발행 불가)"
    if r["ok"] and r["warns"]:
        head = "발행 가능 (권장사항 있음)"
    lines = [f"🔎 [{r['profile']}] 프로필 점검 — {head}"]
    for c in r["checks"]:
        lines.append(f"  {icon.get(c['status'], '•')} {c['msg']}")
    return "\n".join(lines)
