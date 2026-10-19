"""멀티 블로그 프로필 관리 CLI (Phase 3, 2026-07-05).

여러 네이버 블로그(다른 계정·다른 브랜드)를 프로필로 운영한다.
**한 프로세스 = 한 프로필** — 실행할 땐 NBA_PROFILE 로 프로필을 고정하거나 switch 로 .active 지정.

사용:
    python manage_profiles.py list
    python manage_profiles.py create foodblog
    python manage_profiles.py migrate foodblog            # 레거시(default) 데이터 복사(비파괴)
    python manage_profiles.py set-account foodblog --from-json account.json
    python manage_profiles.py switch foodblog             # 활성 프로필 지정(.active)
    python manage_profiles.py show foodblog
    python manage_profiles.py onboard foodblog --from-json account.json   # 생성→계정→안내

set-account 의 account.json 형식(입력 후 반드시 삭제 권장 — 평문):
    {"naver_id": "myid", "naver_pw": "mypw"}
저장 시 Fernet 로 암호화되며 프로필의 config/user_settings.json 에 들어간다(평문 미저장).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

# UTF-8 (한글 출력)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import profile as pf   # noqa: E402
from config import brand as br     # noqa: E402


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_list(_args) -> int:
    active = pf.active_profile()
    _p("=== 프로필 목록 ===")
    for name in pf.list_profiles():
        mark = " ← 활성" if name == active else ""
        d = pf.describe(name)
        acct = d["naver_id_masked"] or ("(자격증명 없음)" if not d["has_credentials"] else "")
        _p(f"  · {name:<16} {acct}{mark}")
    _p(f"\n활성 프로필: {active}  (NBA_PROFILE 또는 switch 로 변경)")
    return 0


def cmd_create(args) -> int:
    if pf.profile_exists(args.name):
        _p(f"이미 존재: {args.name}")
        return 1
    root = pf.create_profile(args.name)
    # 브랜드 스캐폴드(브랜드명 기본값 = 프로필명)
    bj = root / "brand" / "brand.json"
    if not bj.exists():
        bj.write_text(json.dumps({
            "name": args.name, "display": args.name.upper(), "channel": args.name,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    _p(f"✅ 프로필 생성: {root}")
    _p(f"   다음: (1) migrate {args.name}  (2) set-account {args.name}  "
       f"(3) keywords/*.xlsx·brand/presenter/*.png 배치  (4) switch {args.name}")
    return 0


def cmd_migrate(args) -> int:
    plan = pf.plan_migration(args.name)
    exist = [p for p in plan if p["exists"]]
    _p(f"이관 대상(존재하는 것만) {len(exist)}건 → profiles/{args.name}/ (복사, 원본 보존)")
    res = pf.migrate_legacy_to_profile(args.name, move=False, overwrite=args.overwrite)
    _p(f"  복사 {len(res['copied'])} / 스킵 {len(res['skipped'])} / 없음 {len(res['missing'])}")
    return 0


def cmd_set_account(args) -> int:
    data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    nid, npw = data.get("naver_id", ""), data.get("naver_pw", "")
    if not nid or not npw:
        _p("❌ account.json 에 naver_id·naver_pw 필요")
        return 1
    cp = pf.set_account(args.name, nid, npw)
    _p(f"✅ 자격증명 암호화 저장: {cp}  (계정 {pf.describe(args.name)['naver_id_masked']})")
    _p("   ⚠️ 입력한 account.json(평문)은 이제 삭제하세요.")
    return 0


def cmd_switch(args) -> int:
    pf.set_active(args.name)
    _p(f"✅ 활성 프로필 = {pf.active_profile()}  (.active 갱신)")
    _p("   GUI/스케줄러를 이 프로필로 다시 기동하세요(한 프로세스=한 프로필).")
    return 0


def cmd_relocate_default(args) -> int:
    """default 데이터를 profiles/<name>/ 로 재배치(정체성·동작 불변, 데이터 위치만).
    ⚠️ 데이터는 이 명령이 옮기지 않는다 — 먼저 복사돼 있어야 한다(안전 이관 절차)."""
    pf.set_default_data_target(args.name)
    _p(f"✅ default 데이터 재배치 → profiles/{args.name}/  (다음 기동부터 반영)")
    _p("   레거시 원본은 그대로 백업으로 남는다. 되돌리려면: revert-default")
    return 0


def cmd_revert_default(_args) -> int:
    pf.set_default_data_target(None)
    _p("✅ default 데이터 재배치 해제 — 레거시(앱 루트)로 복귀 (다음 기동부터).")
    return 0


def cmd_show(args) -> int:
    name = args.name or pf.active_profile()
    d = pf.describe(name)
    b = br.summary(name)
    _p(f"=== 프로필 {name} ===")
    _p(f"  루트: {d['root']}")
    _p(f"  기본(default): {d['is_default']}")
    if d['is_default']:
        tgt = pf.default_data_target()
        _p(f"  데이터 위치: {'profiles/' + tgt + '/ (재배치됨)' if tgt else '앱 루트(레거시)'}")
    _p(f"  자격증명: {'있음' if d['has_credentials'] else '없음'}  계정: {d['naver_id_masked'] or '-'}")
    _p(f"  쿠키: {'있음' if d['has_cookies'] else '없음'}")
    _p(f"  브랜드: {b['name']} / 표기 {b['display']} / 채널 {b['channel']}")
    _p(f"  진행자 모델: {b['presenter_dir'] or '(없음)'}")
    _p(f"  키워드 엑셀: {b['excel'] or '(없음)'}")
    return 0


def cmd_doctor(args) -> int:
    """프로필 발행 준비 점검(오류 사전 차단). 값 비노출."""
    from config import profile_doctor
    name = args.name or pf.active_profile()
    _p(profile_doctor.report(name))
    return 0 if profile_doctor.diagnose(name)["ok"] else 1


def cmd_onboard(args) -> int:
    _p(f"=== 새 블로그 온보딩: {args.name} ===")
    if not pf.profile_exists(args.name):
        cmd_create(args)
    if args.migrate:
        cmd_migrate(args)
    if args.from_json:
        cmd_set_account(args)
    _p("\n다음 수동 단계:")
    _p(f"  · profiles/{args.name}/keywords/ 에 키워드 엑셀(.xlsx) 배치")
    _p(f"  · profiles/{args.name}/brand/presenter/ 에 진행자 모델 PNG(선택)")
    _p(f"  · profiles/{args.name}/brand/brand.json 브랜드명·도메인 필드 확인")
    _p(f"  · NBA_PROFILE={args.name} 로 GUI 기동 → '🔐 네이버 로그인'으로 쿠키 확보")
    _p(f"  · 준비되면: switch {args.name}")
    # 발행 준비 점검 리포트(무엇이 준비됐고 무엇이 남았는지)
    _p("")
    from config import profile_doctor
    _p(profile_doctor.report(args.name))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="멀티 블로그 프로필 관리")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p = sub.add_parser("create"); p.add_argument("name"); p.set_defaults(func=cmd_create)

    p = sub.add_parser("migrate"); p.add_argument("name")
    p.add_argument("--overwrite", action="store_true"); p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("set-account"); p.add_argument("name")
    p.add_argument("--from-json", required=True); p.set_defaults(func=cmd_set_account)

    p = sub.add_parser("switch"); p.add_argument("name"); p.set_defaults(func=cmd_switch)

    p = sub.add_parser("show"); p.add_argument("name", nargs="?"); p.set_defaults(func=cmd_show)

    p = sub.add_parser("doctor"); p.add_argument("name", nargs="?"); p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("relocate-default"); p.add_argument("name")
    p.set_defaults(func=cmd_relocate_default)
    sub.add_parser("revert-default").set_defaults(func=cmd_revert_default)

    p = sub.add_parser("onboard"); p.add_argument("name")
    p.add_argument("--from-json"); p.add_argument("--migrate", action="store_true")
    p.set_defaults(func=cmd_onboard)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        _p(f"❌ 오류: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
