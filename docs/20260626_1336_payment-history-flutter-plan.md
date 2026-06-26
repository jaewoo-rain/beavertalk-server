# 결제 내역(Payment History) 페이지 Flutter 구현 플랜

작성 2026-06-26. Figma node `2117-20207` → Flutter. **백엔드 변경 없음**(엔드포인트 완비). 목업 교체.

## 한 줄 요약
마이페이지 "Payment History"가 현재 목업(`Routes.subscription`으로 잘못 연결)이다. Figma 결제내역 페이지를 `GET /payments` 실데이터로 새 화면으로 구현해 교체한다.

## 백엔드 계약 (이미 존재 — 변경 X)
- `GET /api/v1/payments?type=all|subscribe|character&page=N` → `PaymentPage`
  - `month_total`(string, 이번 달 합계), `items: PaymentItem[]`, `page`, `size`(=10), `has_more`
  - `PaymentItem`: `payment_id, payment_date(ISO?), description, card_info, price(string?), category`
- `GET /api/v1/subscriptions` → `SubscriptionOut[]` (다음 결제 예정일 = 활성 구독 `end_date`)
- 인증: 공유 `dioProvider`(Supabase Bearer 자동). 베이스 URL `Env.apiBaseUrl` + prefix `/api/v1`.

## Figma 구조 (2117-20207)
1. Header/GNB: back arrow (title 숨김). 기존 헤더 컨벤션 따름.
2. **요약 카드**(bg `#1F222A`, radius 8, padding 16/16/24):
   - "이번 달 결제 금액" (Body1 Regular, white)
   - `month_total` + "$" (Title3 Bold 24, white)
   - row: "다음 결제 예정일" · `7월 1일` (Label1, Secondary `#9EA3B2`) — **활성 구독 end_date**. 없으면 행 숨김.
3. **탭**(Top Navigation, row gap 12): 전체(활성=fill `#252932`) / 구독 / 캐릭터(비활성=outline). → `type` 매핑.
4. **월별 그룹 리스트**(gap 12, width 335):
   - 월 헤더 "YYYY년 M월" (Label1 Regular, Secondary) — `payment_date` 기준 그룹핑.
   - `Card-Line(type=payment)` 행: 좌측 제목(`description`, white) + meta(`M월 D일` · `card_info`, Secondary), 우측 금액(`price$`, Label1 Bold white) + "완료"(Status/Positive `#1ED45A`).

## 재사용 컴포넌트 (기존)
- `lib/components/molecules/card_line.dart` — `CardLine(type: CardLineType.payment, label, meta, value, status)` 그대로 사용.
- 탭: Figma는 fill/outline 버튼 2종. 기존 Button atom 또는 간단 pill 위젯으로 구현(활성 fill `#252932`, 비활성 outline). `SegmentedTabs`도 가능하나 디자인은 개별 버튼.
- 테마: `AppColors`(surface2 `#252932`, textSecondary `#9EA3B2`, success `#1ED45A`), `AppType`(body1/title3/label1).

## 데이터 계층 (call_history 패턴, 단 응답이 객체)
신규 feature: `lib/features/payment/`
1. **Entities** `domain/entities/payment_page.dart`:
   - `PaymentPage { String monthTotal; List<PaymentItem> items; int page,size; bool hasMore; }`
   - `PaymentItem { int paymentId; DateTime? paymentDate; String? description, cardInfo, price, category; }`
2. **DTO** `data/models/payment_page_dto.dart` (snake_case fromJson):
   - `PaymentPageDto.fromJson` → `PaymentItemDto.fromJson`(`payment_id/payment_date/description/card_info/price/category`) → `toEntity()`(price는 string 유지, paymentDate는 DateTime.tryParse).
3. **DataSource** `data/datasources/payment_remote_data_source.dart`:
   - `Future<PaymentPageDto> listPayments({String type='all', int page=1})` → `_dio.get('/payments', queryParameters:{'type':type,'page':page})` (응답은 Map 객체).
4. **Repository** `domain/repositories/payment_repository.dart` + `data/repositories/payment_repository_impl.dart` (`mapDioException`).
5. **Providers** `presentation/payment_providers.dart`:
   - `paymentRemoteDataSourceProvider`, `paymentRepositoryProvider`.
   - `paymentTabProvider = StateProvider<String>('all')`.
   - `paymentPageProvider = FutureProvider.family<PaymentPage,String>((ref,type)=>repo.listPayments(type:type))`.
   - `nextBillingDateProvider = FutureProvider<DateTime?>` (활성 구독 end_date; subscriptions 데이터소스 경량 추가 또는 기존 재사용).

## UI 작업
- `lib/screens/mypage/payment_history.dart` 신규:
  - 헤더 + 요약 카드(`month_total` from selected-tab page, next date from `nextBillingDateProvider`).
  - 탭 3개(`paymentTabProvider` 갱신 → `paymentPageProvider(tab)` 재조회).
  - 본문 `AsyncValue.when(loading/error/data)`. data → `items`를 월별로 그룹핑 후 월헤더 + `CardLine` 리스트. 빈 상태 처리.
  - 금액 포맷 `"$price"`(예 `12.9$`), 날짜 `M월 D일`, 월 `YYYY년 M월`, status 상수 "완료".
- 라우트: `lib/app/routes.dart`에 `Routes.paymentHistory='/mypage/payments'` + onGenerateRoute 등록.
- `lib/screens/mypage/mypage.dart` line 92: `route: Routes.paymentHistory`로 교체.

## 검증
- `flutter analyze` clean.
- 탭 전환 시 재조회, 빈/에러/로딩 상태, month_total·다음결제일 표시, 월 그룹핑.

## 미해결/주의
- "완료" status는 백엔드 `PaymentItem`에 필드 없음 → 상수 표기(목록은 완료 결제만).
- "다음 결제 예정일"은 `PaymentPage`에 없음 → 활성 구독 end_date. 구독 없으면 행 숨김.
- 통화는 bare list, 결제는 `PaymentPage` 객체 — DTO 형태 다름(주의).
