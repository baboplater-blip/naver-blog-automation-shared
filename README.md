# 네이버 블로그 자동화 — 글 작성부터 예약발행까지

글 작성·이미지 생성·품질 검수·초안 관리·즉시발행·예약발행을 지원하는 Windows 데스크톱 프로그램입니다. 공유본에도 네이버 블로그에 글을 올리고 지정한 날짜와 시간에 예약발행하는 기능이 포함되어 있습니다.

## 주요 기능

- 키워드·글감을 바탕으로 AI 글 작성과 이미지 생성
- 글 품질·중복 검사 및 초안 보관·관리
- 네이버 블로그 즉시발행 및 날짜·시간을 지정한 예약발행
- 예약 시간대에 따른 발행 시각 배정
- 여러 블로그 프로필 관리 및 설정에 따른 무인 운영

## 공유본에 포함되는 내용

이 저장소는 데스크톱 앱의 소스 사본입니다. 원본 Git 이력, 사용자 프로필, 설정, 브라우저 세션, 로그, 초안, 엑셀, 이미지, 백업, 테스트, 비공개 오케스트레이션 파일은 포함하지 않습니다. 개인 편집 전략과 일부 선택 기능용 자료도 빠져 있어 해당 기능은 사용자가 직접 설정해야 할 수 있습니다.

## Windows 설치

Python 3.10 이상과 Git을 설치한 뒤 PowerShell에서 실행합니다.

```powershell
git clone https://github.com/baboplater-blip/naver-blog-automation-shared.git
cd naver-blog-automation-shared
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r naver_blog_v3/requirements.txt
py tools\bootstrap_shared.py
```

## 실행

`naver_blog_v3\run_shared_gui.bat`를 실행하면 GUI가 열립니다. CLI는 `naver_blog_v3\run_shared_cli.bat`로 실행할 수 있습니다.

처음 부트스트랩할 때 중립 설정 예시를 새 `naver_blog_v3/config/settings.py`로 복사합니다. 이미 설정 파일이 있으면 그대로 둡니다. 실행기는 `NBA_PROFILE=public-share`를 고정하고 프로필 데이터를 체크아웃의 `profiles/public-share/` 아래에 저장합니다. 기존 `%APPDATA%` 프로필이나 다른 설치의 데이터를 가져오지 않습니다. 각 사용자는 본인 계정과 브라우저 로그인 정보를 직접 설정해야 합니다. 쿠키·프로필·키·`settings.py`를 다른 설치에서 복사하지 마세요.

계정 연결과 예약 설정을 완료하면 GUI에서 즉시발행·예약발행을 사용할 수 있습니다. 무인 자동 실행과 이미지 생성 등은 초기 설정에서 꺼져 있으므로 사용할 기능을 직접 설정하세요.

CLI에서는 실행 옵션으로 초안 저장과 실제 발행을 선택합니다.

```powershell
# 글을 생성해 초안으로 보관
naver_blog_v3\run_shared_cli.bat

# 발행 실행: 예약 설정에 따라 예약발행 또는 즉시발행
naver_blog_v3\run_shared_cli.bat --publish
```

GUI에서도 발행을 직접 실행할 수 있습니다. 공유본 CLI는 옵션 없이 실행하면 초안을 보관하고, `--publish`가 있으면 발행 경로를 실행합니다.

## 검증 범위

공유본의 개인정보 제외, 초기 설정 생성, 모듈 불러오기와 실행 옵션을 확인했습니다. 공유본으로 실 네이버 계정 로그인·실제 발행·유료 서비스·실시간 이미지 생성은 검증하지 않았습니다. 별도 라이선스는 제공하지 않습니다. 재배포나 상업적 이용 전 권리 조건을 확인하세요.

## 수정 후 공유 전 검사

수정한 공유본은 저장소 루트에서 `py privacy_scan.py`로 검사하세요. Git 저장소에서는 추적 파일과 ignore되지 않은 새 파일을 검사하고, ZIP으로 받은 경우에는 전체 배포 파일을 검사합니다. 발견 위치·분류만 출력하며 값은 출력하지 않습니다. 개인 경로·자격증명의 일반적인 패턴, 민감 파일, manifest 불일치를 검사하는 보조 도구이므로 모든 비밀의 탐지를 보장하지 않습니다. 생성된 설정·프로필은 Git에서 제외되며, ZIP으로 재공유할 때는 포함하지 마세요.
