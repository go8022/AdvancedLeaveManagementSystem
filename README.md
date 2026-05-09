# Advanced Annual Leave Management System (ALMS)

Windows 10/11 + Python 3.x용 로컬 GUI 연차 관리 프로그램입니다.

## 설치

Python이 설치되어 있지 않다면 먼저 Python 3.x를 설치하고, 설치 시 `Add python.exe to PATH`를 선택하세요.

```powershell
py -m pip install cryptography holidays plyer reportlab
```

실행:

```powershell
py alms_app.py
```

`python` 명령이 PATH에 등록되어 있다면 다음도 가능합니다.

```powershell
python alms_app.py
```

```pyinstaller
옵션 의미:
--onefile: exe 하나로 묶음
--windowed: 콘솔창 없이 GUI만 실행
--name ALMS: exe 이름을 ALMS.exe로 지정
현재 앱은 실행 폴더에 alms_secure.dat, alms_secure.key를 생성하므로, ALMS.exe를 원하는 폴더에 두고 실행하면 그 폴더 기준으로 데이터 파일이 만들어집니다.
아이콘까지 넣고 싶으면 .ico 파일을 준비한 뒤:

py -m PyInstaller --onefile --windowed --name ALMS --icon alms.ico alms_app.py
```


## 주요 기능

- 사번, 비밀번호, 이름, 입사일자 기반 사용자 등록/로그인
- 사용자 개인정보와 휴가 데이터의 `.dat` 암호화 저장
- 입사일 기준 회계연도 자동 계산 및 범위 밖 날짜 Dimmed 처리
- 국내 일반 연차 산식 기반 자동 추정
- 최종 연차 일수 0.5일 단위 수동 보정
- 전일, 오전반차, 오후반차 입력과 1일 8시간/반차 4시간 동시 계산
- 월간 달력에서 클릭 또는 드래그로 날짜 선택
- 휴가종류, 신청모드, 계획/실시완료 상태 입력
- 전일 `L`, 오전반차 `H-am`, 오후반차 `H-pm` 표시
- 메모 10자 이내 추가/수정/삭제
- 주말/공휴일 휴가 계획 시 경고 팝업
- 예정 휴가에 대한 앱 실행 중 알림
- 현재 회계연도의 12개월 달력을 A4 1페이지 PDF로 Documents 폴더에 저장

## 연차 계산 기준

코드의 `LeaveEngine.estimated_entitlement_days()`는 다음 일반 기준으로 계산합니다.

- 1년 미만: 입사 후 개근 월수 기준, 최대 11일
- 1년 이상: 15일
- 3년차부터 2년마다 1일 가산
- 최대 25일 (국내 법규 기준이나, 수동으로 25일 이상 기입 가능)

회사 내규가 다르면 화면 우측의 `최종 연차 일수`에서 0.5일 단위로 보정할 수 있습니다.

## .dat 파일 보안 구조

프로그램은 실행 폴더에 다음 파일을 생성합니다.

- `alms_secure.dat`: 사용자 정보, 입사일, 휴가 계획, 메모가 들어 있는 암호화 데이터 파일
- `alms_secure.key`: `.dat` 파일 복호화에 필요한 Fernet 대칭키

저장 흐름:

1. 사용자 데이터는 JSON 구조로 구성됩니다.
2. 비밀번호는 원문 저장하지 않고 PBKDF2-HMAC-SHA256 해시와 salt로 저장됩니다.
3. 전체 JSON은 `cryptography.fernet.Fernet`으로 암호화됩니다.
4. 암호문만 `alms_secure.dat`에 저장됩니다.

주의:

- `alms_secure.dat`만으로는 내용을 읽기 어렵지만, `alms_secure.key`와 함께 있으면 복호화가 가능합니다.
- 두 파일을 모두 외부에 공유하지 마세요.
- 파일 손상 시 기존 `.dat` 파일은 `.dat.broken`으로 분리되고 새 데이터 파일을 생성합니다.

## 코드 구조

- `AuthManager`: 암호화 저장, 회원가입, 로그인, 비밀번호 검증
- `LeaveEngine`: 입사일 기준 회계연도, 연차 부여일수, 사용/잔여 시간 계산, 공휴일 판단
- `CalendarUI`: Tkinter GUI, 월간 달력, 휴가/메모 입력, 대시보드, PDF 출력
- `NotificationService`: 계획된 휴가 미리알림

## 알려진 운영 참고사항

- 법정공휴일은 `holidays` 패키지의 한국 공휴일 데이터를 사용합니다.
- 시스템 트레이 알림은 `plyer`가 지원되는 환경에서 동작하며, 실패 시 팝업으로 대체됩니다.
- PDF 출력은 `reportlab`이 필요합니다.
