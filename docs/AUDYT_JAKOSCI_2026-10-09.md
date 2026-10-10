# Audyt jakości Legion Go 2 Companion

## Instalacja najnowszej lokalnej paczki — 2026-10-10

Po zakończeniu audytów zainstalowano na Legion Go 2 paczkę **1.0.4** o SHA-256 `c9c41c3e35fd5fc1e8e3e373ad7d8503383c63f3712f6ffde93f34d747ff3b71`, zawierającą poprawkę L1. Zweryfikowano zgodność wszystkich **85 plików** instalacji z archiwum. Zewnętrzne ustawienia pozostały identyczne bajtowo; zachowano kopię poprzedniej instalacji i ustawień.

Po przeładowaniu wyłącznie Companion przez Decky wszystkie **8 modułów** zgłosiło stan ready, bez błędów startu, konfliktów ani żądania restartu. Steam CEF potwierdził załadowanie interfejsu `Content`; serwowany frontend ma SHA-256 `3a0643d0cbbc4c32f33735f1950e5af995035e6eb06b4ffec735c69e3dc819a3`. To sprawdzenie instalacji i startu nie zastępuje fizycznego write/readback każdej funkcji ani testów POSIX pominiętych na Windows.

Poniższe wyniki i ograniczenia opisują stan w chwili poszczególnych audytów. W szczególności zapis dwunastego audytu o braku L1 na konsoli został zastąpiony opisaną powyżej instalacją.

## Dwunasty, świeży audyt — 2026-10-10

**AUDYT CZYSTY — 0 potwierdzonych problemów do naprawy.** Od początku tej osobnej sesji nie znaleziono nowej potwierdzonej usterki ani uzasadnionej optymalizacji wymagającej zmiany kodu. W tej sesji znaleziono **0** i naprawiono **0** problemów; pozostałe znane, potwierdzone problemy wymagające lokalnej naprawy: **0**. Nie dodawano zmian runtime ani testów bez potrzeby.

Przegląd objął wszystkie osiem modułów, wydajność i responsywność, kolejki oraz dopuszczanie RPC, współdzielone zasoby i ownership, start/unload, anulowanie zadań, zapis i recovery ustawień, obsługę błędów, zakresy profili TDP/CPU i wibracji, polling i widoczność stron, opóźnione odpowiedzi, debounce i zapis przy zamknięciu, interfejs frontend–backend, aktualizacje, zależności, build i pakowanie. Sprawdzono również poprzednią poprawkę L1 i jej integrację z blokadą konfliktów.

### Wyniki tej sesji

- Backend: **568 testów, 550 zaliczonych, 18 pominiętych na Windows**, bez błędów.
- Wszystkie **11 zestawów frontendu** zaliczone, w tym 29 scenariuszy recovery TDP. Strict TypeScript z unused/unreachable checks: zaliczony.
- Świeży build, walidator bundle i kontrola payloadu **40 wpisów**: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Istniejący ZIP **1.0.4** sprawdzono ponownie walidatorem pobierania i dokładnym walidatorem CI: poprawne CRC, ścieżki, kompletność, licencje i zgodność wszystkich plików z aktualnym checkoutem oraz świeżym buildem. Wszystkie **44 moduły Python w ZIP** kompilują się.
- Paczka pozostaje bez zmian: **85 plików, 14 katalogów, 494 902 bajty**, SHA-256 `c9c41c3e35fd5fc1e8e3e373ad7d8503383c63f3712f6ffde93f34d747ff3b71`. Nowe pakowanie nie było potrzebne, ponieważ nie zmieniono żadnego dostarczanego pliku.
- Aktualny JavaScript i mapa są identyczne bajtowo z niezależnym buildem odtworzonym z ZIP w poprzedniej sesji. W tej sesji ponownie sprawdzono tę zgodność, bez ponownej instalacji zależności w katalogu odtworzenia.
- `git diff --check`: zaliczony. HEAD bez zmian: `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

### Walidacja sprzętowa i ograniczenia

W tej sesji nie korzystano z fizycznej konsoli ani Steam CEF, nie instalowano plugina i nie restartowano Decky. Nie wykonano fizycznego write/readback ani pominiętych na Windows testów Linux/POSIX. Zdalnego CI nie uruchamiano. Te niewykonane sprawdzenia pozostają ograniczeniami walidacji i nie są wykazywane jako zaliczone. Wcześniej zainstalowana na Legionie paczka nie zawiera poprawki L1 z jedenastego audytu.

Wcześniejsze lokalne poprawki zachowano. **Bez commita i pusha.** Jedyną nową zmianą tej sesji jest niniejszy zapis wyniku audytu; raport nie trafia do ZIP-a.

## Jedenasty audyt i naprawy — 2026-10-10

Świeży audyt aktualnego kodu wykrył **1 potwierdzony problem L1**, który naprawiono w tej sesji. Dodano **2 testy regresji**; oba odtwarzały błąd przed poprawką i przechodzą po niej. Ponowny przegląd po naprawie nie wykazał kolejnej potwierdzonej usterki wymagającej lokalnej zmiany. Nie jest to audyt czysty od pierwszego przebiegu.

| ID | Priorytet | Problem i naprawa | Kod |
| --- | --- | --- | --- |
| L1 | P2 | Manifest innego plugina o rozmiarze mieszczącym się w limicie, lecz z bardzo głębokim zagnieżdżeniem JSON, powodował `RecursionError`. Ten wyjątek omijał obsługę błędów skanowania, mógł kończyć monitor konfliktów i pozostawiać działające moduły bez ustawienia blokady. Dekoder zgłasza teraz błąd weryfikacji manifestu, który istniejąca bramka obsługuje przez zatrzymanie modułów i utrzymanie blokady do restartu procesu. | `conflict_guard.py`, `tests/test_conflict_guard.py` |

### Zakres i dowody

- Przegląd objął wszystkie osiem modułów, TDP/CPU i zakresy profili, zapis i recovery ustawień, start/unload i anulowanie zadań, ownership oraz współdzielenie InputPlumber, odczyty i kolejki RPC, polling i widoczność stron, menu główne, obsługę konfliktów, aktualizacje oraz build i pakowanie.
- Regresja detektora używa rzeczywistego pliku JSON z 2000 poziomami tablic, o rozmiarze znacznie mniejszym niż dopuszczalne 64 KiB. Regresja integracyjna uruchamia osiem atrap modułów, uszkadza manifest podczas pracy i sprawdza zatrzymanie każdego modułu, odrzucenie dalszego RPC sprzętowego oraz dostępność statusu po naprawie pliku. Kontrola konfliktu nadal wymaga świeżego procesu do wznowienia pracy.
- Kontrola blokujących wywołań w funkcjach async obejmowała również helpery i ich kontekst wykonania: transakcja WiFi wykonuje I/O w osobnym pracowniku; lokalne odczyty sysfs/proc i zegara nie są traktowane jako polecenia sprzętowe ani dowód opóźnienia. Nie potwierdzono dalszej optymalizacji uzasadniającej zmianę zachowania.

### Weryfikacja

- Backend przed naprawą: **566 testów, 548 zaliczonych, 18 pominiętych na Windows**. Dwa nowe testy osobno odtworzyły `RecursionError` przed poprawką.
- Backend po naprawie: **568 testów, 550 zaliczonych, 18 pominiętych na Windows**, bez błędów; wszystkie 16 testów kontroli konfliktów zaliczone.
- Wszystkie **11 zestawów frontendu**, strict TypeScript z kontrolą unused/unreachable, build, walidator bundle i kontrola payloadu: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Przebudowany ZIP **1.0.4**: **85 plików, 14 katalogów, 494 902 bajty**. Walidator pobierania oraz dokładny walidator CI potwierdziły CRC, ścieżki, licencje, kompletność i zgodność wszystkich plików z checkoutem.
- SHA-256 ZIP: `c9c41c3e35fd5fc1e8e3e373ad7d8503383c63f3712f6ffde93f34d747ff3b71`.
- Build odtworzono z finalnego ZIP w osobnym katalogu po `npm ci --ignore-scripts` z dołączonego lockfile. Strict TypeScript, build, walidator bundle i payload check: zaliczone. JavaScript i mapa źródeł są identyczne bajtowo z lokalnymi artefaktami. `git diff --check`: zaliczony.

### Ograniczenia i stan pracy

Nie ma pozostałego znanego, potwierdzonego problemu wymagającego lokalnej poprawki w zakończonym zakresie. W tej sesji nie wykonywano testów na sprzęcie, instalacji na Legionie, restartu Decky ani sprawdzenia Steam CEF. Testy Linux/POSIX pominięte na Windows pozostają niewykonane; zdalnego CI nie uruchamiano. Wcześniejsza instalacja na konsoli nie obejmuje nowej poprawki L1. Brak aktualnej walidacji sprzętowej nie jest zaliczonym testem ani potwierdzonym błędem.

Wszystkie wcześniejsze poprawki pozostają zachowane. **Bez commita i pusha**; HEAD: `61bad19bde08d937c4678ddc73c3367401b0a0ec`. Poniżej zachowano wyniki wcześniejszych sesji.

## Dziesiąty audyt i naprawy — 2026-10-10

Ponowny pełny lokalny audyt jakości i responsywności zakończono naprawą **4 potwierdzonych problemów K1–K4**. Przegląd objął TDP/CPU, wibracje, OLED, WiFi, RGB, remapper, baterię i kontroler, ich lifecycle i współdzielone zasoby, zapis oraz recovery ustawień, odczyty statusu, kolejki RPC, zamykanie i ponowne otwieranie stron, menu główne, aktualizacje, zależności, build i pakowanie. Potwierdzone problemy naprawiano z regresjami i ponownie przeglądano kod. Po ostatniej poprawce i pełnej weryfikacji nie znaleziono dalszej potwierdzonej usterki wymagającej lokalnej zmiany.

**W zakresie zakończonego lokalnego audytu nie ma już niczego do naprawy.** Weryfikacja działania na fizycznej konsoli, w Steam CEF oraz pominiętych operacji Linux/POSIX pozostaje niewykonana.

**Bez commita, pusha, instalacji i zapisów na konsoli.** Zachowano wszystkie wcześniejsze lokalne poprawki i zakres funkcji Companion. HEAD: `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Problem i zastosowana naprawa | Kod |
| --- | --- | --- | --- |
| K1 | P2 | Zmiana ochrony baterii lub źródła gyro przyjęta przed zamknięciem strony mogła jeszcze czekać na jej wcześniejszy odczyt. Nowa strona rozpoczynała wtedy własny odczyt przed tym zapisem i pokazywała nieaktualny stan aż do następnego pollingu. Każdy z tych modułów ma teraz kolejkę przyjętych zmian wspólną dla instancji strony; nowy snapshot czeka na jej zakończenie i ponownie sprawdza widoczność oraz rewizję. | [battery.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/battery.tsx>), [controller.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/controller.tsx>) |
| K2 | P2 | Pierwszy snapshot ponownie otwartej strony wibracji omijał globalną kolejkę sliderów zaakceptowanych przez poprzednią stronę. Inicjalizacja czeka teraz na cały ogon zapisów przed odczytem ustawień; zamknięta próba nie uruchamia dalszych odczytów po tym oczekiwaniu. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx>) |
| K3 | P2 | Konflikt i unload synchronicznie wywoływały `CompanionUpdates.close()`, które może czekać na blokadę trwającej publikacji ZIP-a. Ten wait mógł zatrzymać cały event loop. Bariera działa teraz w zadaniu roboczym; unload obejmuje ją swoim chronionym cleanupem, także po wielokrotnym anulowaniu. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py>) |
| K4 | P2 | Odczyty podsumowań w menu głównym omijały bramki zapisów stron baterii, gyro, remappera, RGB i wibracji. Publiczne gettery czekają teraz na wcześniej przyjęte zmiany danego modułu. Weryfikacyjny odczyt baterii wewnątrz transakcji używa osobnej funkcji, aby nie czekać na zakończenie własnej transakcji. | [index.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/index.tsx>), [battery.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/battery.tsx>), [controller.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/controller.tsx>), [remap.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/remap.tsx>), [rgb.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx>), [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx>) |

### Dowody i responsywność

- Kontrolowany wcześniejszy odczyt i zapis baterii/gyro potwierdzają, że ponowne otwarcie nie dodaje wyprzedzającego snapshotu. Po sukcesie lub błędzie zapisu właściwy odczyt rusza od razu, bez oczekiwania na timer 10 s. Ukryta strona nadal nie odczytuje sprzętu po zwolnieniu kolejki.
- Dwa różne suwaki wibracji zapisane przy zamknięciu kończą się przed nowym snapshotem. Sprawdzono udany zapis i odrzucenie drugiego RPC; UI pokazuje świeży potwierdzony profil.
- Publiczne gettery wszystkich pięciu modułów nie rozpoczynają RPC podsumowania przed zakończeniem przyjętych zmian. Niezależne podsumowania i odczyt baterii kontrolerów pozostają osobnymi kanałami.
- Regresja baterii z odpowiedzią sukcesu bez dołączonego statusu potwierdza wykonanie odczytu weryfikacyjnego, zakończenie transakcji i dopiero potem odczyt menu, bez wzajemnego oczekiwania.
- Celowo opóźnione zamknięcie aktualizatora pozwala heartbeatowi asyncio wykonać się podczas oczekiwania. Przed poprawką heartbeat nie mógł się wykonać. Dwukrotne anulowanie unload nie porzuca zamknięcia aktualizatora ani cleanupu żadnego z ośmiu modułów.
- Ponowna kontrola blokujących wywołań w async backendach nie ujawniła dalszego publicznego odczytu ustawień ani subprocessu na event loop. Prywatna transakcja WiFi nadal wykonuje swoje I/O wewnątrz pracownika. Sprawdzono również ownership i przywracanie OLED, TDP/CPU, remappera, gyro, RGB i baterii, recovery plików oraz walidację aktualizacji.

Są to lokalne dowody kolejności, braku nadmiarowych odczytów i responsywności pętli. Nie są pomiarem opóźnienia na fizycznym Legion Go 2.

### Weryfikacja końcowa

- Dodano **3 testy backendu** w [test_ninth_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_ninth_audit.py>) oraz **12 scenariuszy frontendu**: 9 w [frontend_async_polling.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_async_polling.cjs>) i 3 w [frontend_vibration_recovery.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_vibration_recovery.cjs>).
- Backend: **566 testów, 548 zaliczonych, 18 pominiętych na Windows**, bez błędów. Wszystkie **11 zestawów frontendu zaliczone**.
- Strict TypeScript wraz z unused/unreachable checks, build i walidator bundle: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Finalny ZIP: **85 plików, 14 katalogów, 494 776 bajtów**. Dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje i zgodność zawartości z checkoutem.
- SHA-256 ZIP: `0190aed2f4e4068fbc5ad4e9e09a3d0ccb61e8fe1a9874149ba0538883c6f470`.
- Build odtworzono z rozpakowanego finalnego ZIP po `npm ci --ignore-scripts` z dołączonego lockfile. Strict TypeScript, walidator bundle i payload check: zaliczone. JavaScript i mapa źródeł są **identyczne bajtowo** z lokalnym buildem.
- SHA-256 odtworzonego JavaScript: `3a0643d0cbbc4c32f33735f1950e5af995035e6eb06b4ffec735c69e3dc819a3`; mapy: `fe8cc72e01621b7d501d6082ea7691322fcb8ff7106630ff325ab97ce6ba12e0`.
- Instalacja zależności nadal emituje komunikat deprecacji developerskiego `glob@10.5.0`; bieżący skan advisories nie wykazuje podatności. Ta zależność nie trafia do runtime plugina.
- `git diff --check`: zaliczony. HEAD bez zmian; nie wykonano commita ani pusha. Zdalnego CI nie uruchamiano.

Poniżej zachowano wcześniejsze audyty i ich ówczesne wyniki.

## Dziewiąty audyt i naprawy — zapis historyczny z 2026-10-10

Pełny lokalny audyt jakości i responsywności zakończono naprawą **7 potwierdzonych problemów J1–J7**. Przegląd objął wszystkie osiem modułów: TDP/CPU, wibracje, OLED, WiFi, RGB, remapper, baterię i kontroler; ponadto lifecycle, współdzielenie zasobów, kolejki RPC, polling i widoczność stron, ustawienia i recovery, kontrolę konfliktów, aktualizacje, zależności oraz build i pakowanie. Po kolejnych poprawkach powtarzano kontrolę zmienionych ścieżek i ich otoczenia. Ostatni przebieg nie ujawnił dalszej potwierdzonej usterki wymagającej lokalnej poprawki.

**W zakresie zakończonego lokalnego audytu nie ma już niczego do naprawy.** Nie jest to potwierdzenie działania na fizycznym urządzeniu ani zastępstwo weryfikacji Steam CEF i operacji Linux/POSIX pominiętych na Windows.

**Bez commita, pusha, instalacji i zapisów na konsoli.** Wszystkie wcześniejsze lokalne poprawki pozostają zachowane. HEAD: `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Problem i zastosowana naprawa | Kod |
| --- | --- | --- | --- |
| J1 | P1 | Końcowe wycofanie legacy OLED korzystało z cache wyjścia sprzed dockowania i mogło zmienić HDR na wyświetlaczu zewnętrznym albo w nowej sesji gamescope. Teraz odczytuje świeże wyjście i sprawdza tożsamość związanej sesji przed i po odczycie. Nieznane wyjście zachowuje pending cleanup; recovery przed związaniem sesji usuwa własny skrypt bez zmiany atomów tej sesji. | [module_control.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/module_control.py>) |
| J2 | P2 | Helper RyzenAdj sprawdzał końcowy URL dopiero po automatycznym wykonaniu przekierowań. Handler sprawdza każdy kolejny adres przed otwarciem: HTTPS, dozwolony host, brak credentials i standardowy port. Końcowa kontrola oraz zamknięcie odrzuconej odpowiedzi pozostają aktywne. | [tdp_updater.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_updater.py>) |
| J3 | P2 | Ponowne otwarcie remappera mogło wyprzedzić zaakceptowane zapisy poprzedniej strony, a RGB odczytywać stan przed końcem swojej wspólnej kolejki. Remapper ma teraz kolejkę wspólną dla instancji; oba moduły czekają na cały zaakceptowany ogon zapisów przed nowym snapshotem i ponownie sprawdzają zamontowanie, widoczność oraz rewizję. | [remap.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/remap.tsx>), [rgb.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx>) |
| J4 | P2 | Pierwszy błąd `Promise.all` w inicjalizacji wibracji pozwalał ponowić wszystkie RPC mimo nadal trwającego odczytu sterownika lub capabilities. Błąd jest pokazywany od razu, a ponowienie czeka na zakończenie pozostałych odczytów poprzedniej próby. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx>) |
| J5 | P2 | Pierwszy aktywny profil WiFi był wybierany niezależnie od pierwszego interfejsu. Przy dodatkowym adapterze można było odczytywać lub zmieniać profil innego urządzenia. UUID pochodzi teraz z wybranego interfejsu i jest walidowany; odłączony interfejs nie pożycza innego profilu. Sprawdzenie aktywności szuka konkretnego UUID we wszystkich aktywnych profilach. | [wifi_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/wifi_backend.py>) |
| J6 | P2 | Manifest sąsiedniego plugina będący FIFO oraz podmiana zwykłego pliku ustawień na FIFO między `lstat` i `open` mogły zawiesić odczyt metadanych. Otwarcie używa `O_NONBLOCK`, a manifest musi być zwykłym plikiem według otwartego deskryptora przed odczytem. Deskryptory są zamykane również po błędzie. | [conflict_guard.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/conflict_guard.py>), [safe_settings.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/safe_settings.py>) |
| J7 | P2 | Po awarii międzyprocesowej blokady WiFi publiczny async RPC synchronicznie odczytywał ustawienia. Recovery mogło zablokować event loop albo przesłonić pierwotny błąd. Odpowiedź zachowuje przyczynę awarii bez dodatkowego odczytu i bez rozpoczęcia mutacji. | [wifi_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/wifi_backend.py>) |

### Dowody i responsywność

- Testy OLED odtwarzają cache sprzed dockowania, zmianę sesji podczas świeżego odczytu, nieznane wyjście, recovery bez związanej sesji i poprawne wycofanie dla panelu wewnętrznego. Nowa lub niezwiązana sesja nie otrzymuje atomów legacy.
- Test z rzeczywistym mechanizmem przekierowań `urllib` i atrapą transportu potwierdza, że obcy pośredni URL nigdy nie jest otwierany. Sprawdzono również poprawne przekierowanie i zamknięcie odpowiedzi odrzuconej przez końcową kontrolę.
- Dla remappera i RGB kontrolowane dwa zapisy starej strony kończą się przed pierwszym odczytem nowej. Błąd starego zapisu i ukrycie czekającej strony nie blokują późniejszego odświeżenia; po pokazaniu odczyt rusza bez oczekiwania na następny okres pollingu.
- Wibracje pokazują pierwszy błąd bez oczekiwania na pozostałe RPC. Retry nie dodaje żadnych duplikatów, gdy poprzedni odczyt sterownika lub capabilities nadal trwa; po jego zakończeniu nowa próba działa.
- WiFi używa `nmcli -g GENERAL.CON-UUID device show <iface>`, zgodnie z [oficjalnymi przykładami NetworkManager](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli-examples.html). Test z dwoma adapterami potwierdza wybór właściwego profilu, odrzucenie braku lub niepoprawnego UUID oraz ponowne wykorzystanie raz odkrytego interfejsu.
- Kontrola metadanych odrzuca symulowany deskryptor FIFO przed jakimkolwiek odczytem i go zamyka. Test flag otwarcia chroni przed blokowaniem po podmianie pliku. Rzeczywiste FIFO wymaga weryfikacji POSIX.
- Awaria blokady WiFi zachowuje pierwotny komunikat i nie wykonuje odczytu ustawień ani mutacji. Test przed poprawką odtwarzał przesłonięcie błędu przez odczyt na event loop.

Są to lokalne dowody kolejności, liczby operacji i zachowania po błędach. Nie wykonano pomiarów opóźnień na fizycznym Legion Go 2. Przegląd blokujących wywołań rozróżnia publiczny event loop od prywatnych transakcji WiFi uruchamianych celowo wewnątrz zadania roboczego; tych transakcji nie przenoszono na pętlę główną.

### Weryfikacja końcowa

- Dodano **14 testów backendu** w [test_eighth_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_eighth_audit.py>) i **6 scenariuszy frontendu**: 4 w [frontend_async_polling.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_async_polling.cjs>) oraz 2 w [frontend_vibration_recovery.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_vibration_recovery.cjs>).
- Backend: **563 testy, 545 zaliczonych, 18 pominiętych na Windows**, bez błędów. Wszystkie **11 zestawów frontendu zaliczone**.
- Strict TypeScript wraz z unused/unreachable checks, build i walidator bundle: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Finalny ZIP: **85 plików, 14 katalogów, 493 048 bajtów**. Dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje i zgodność zawartości z checkoutem.
- SHA-256 ZIP: `18614bb62e5ec5ad4eabd5b33697554394840a14dab59a881029a36f26ca451f`.
- Build odtworzono z rozpakowanego finalnego ZIP po `npm ci --ignore-scripts` z dołączonego lockfile. Strict TypeScript, walidator bundle i payload check: zaliczone. `dist/index.js` i mapa źródeł są **identyczne bajtowo** z lokalnym buildem.
- SHA-256 odtworzonego JavaScript: `59ecb9cc4c25975a9e6aa28eeaf06a7b36c9bb00bc6972df65c7b5347018c942`; mapy: `1efe6301c796736684b07f8a832f022e52de7db4f2c1d8031244f95df1b9185d`.
- Instalacja zależności emituje komunikat deprecacji `glob@10.5.0`, używanego przez narzędzie developerskie `@rollup/plugin-commonjs`; bieżący skan advisories nie wykazuje podatności. Nie jest to zależność runtime plugina.
- `git diff --check`: zaliczony. HEAD bez zmian; nie wykonano commita ani pusha. Zdalnego CI nie uruchamiano.

Poniżej zachowano wcześniejsze audyty i ich ówczesne wyniki.

## Ósmy audyt i naprawy — zapis historyczny z 2026-10-10

Kolejny pełny lokalny audyt jakości i responsywności zakończono naprawą **4 potwierdzonych problemów I1–I4**. Przegląd objął osiem modułów, ich lifecycle, kolejki i współdzielone zasoby, status i polling UI, zapis oraz recovery ustawień, OLED, helper i updater, zależności oraz build i pakowanie. Nowe przypadki potwierdzono testami przed poprawkami; wykonano również kontrolę ich integracji po naprawie.

**Bez commita, pusha, instalacji i zapisów na konsoli.** Wcześniejsze poprawki pozostają zachowane. HEAD: `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Problem i wynik naprawy | Kod |
| --- | --- | --- | --- |
| I1 | P2 | Remapper podczas unload/uninstall wywoływał `_load_state()` jako argument `offload`, więc recovery lub oczekiwanie na blokadę ustawień odbywało się na event loop. Cały odczyt i restore są teraz wykonywane w zadaniu roboczym. | [remap_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:548>) |
| I2 | P1 | Snapshot własności remappera był pobierany przed blokadą operacji. Kończący się pracownik monitora mógł zastosować i zapisać nowsze mapowanie, którego restore oparty na starym snapshocie nie rozpoznawał. Odczyt odbywa się pod tą samą blokadą co mutacje, po zakończeniu wcześniejszej pracy. Regresja potwierdza przywrócenie całego pierwotnego profilu po takiej zmianie. | [remap_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:548>) |
| I3 | P2 | `Promise.all` wiązało stan baterii konsoli z odczytem baterii kontrolerów. Powolny HID wstrzymywał pokazanie i użycie ochrony baterii; błąd odczytu konsoli zwalniał wspólną bramkę mimo nadal trwającego odczytu kontrolerów, co pozwalało dublować te żądania. Dwa odczyty mają teraz osobne bramki i rewizje. Konsola działa niezależnie, a odpowiedzi po ukryciu, zamknięciu lub starszym odczycie są odrzucane. | [battery.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/battery.tsx:127>) |
| I4 | P2 | Test wibracji mógł wybrać dowolny inny pad przez fallback lub cache ponownie użytego numeru `event`. Wymaga teraz VID/PID odkrytego HID, sprawdza cache oraz tożsamość otwartego deskryptora przed uploadem i outputem. Probuje tylko pasujące endpointy. Efekt jest jedną transakcją roboczą z zatrzymaniem, usunięciem i zamknięciem, również po błędzie oraz wielokrotnym anulowaniu. | [vibration_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:600>) |

### Dowody i responsywność

- Wstrzymany odczyt ustawień remappera pozwala na heartbeat pętli asyncio. Restore czekający na poprzednią operację używa nowszej własności, a rzeczywisty generator i parser profilu przywracają cały wyjściowy YAML.
- Powolny odczyt kontrolerów nie wstrzymuje pokazania ani zmiany ochrony baterii konsoli. Błąd konsoli i ponowienie nie dodają drugiego oczekującego odczytu HID. Po ponownym otwarciu strony każdy kanał odświeża się niezależnie, kiedy zwolni się jego bramka.
- Dla obcego pada i właściwego kontrolera stary finder wykonywał dwa capability probe; obecny wykonuje jeden, tylko dla zgodnego VID/PID. Poprawny cache nadal pomija skan i capability ioctl.
- Symulacja ponownie użytego deskryptora potwierdza brak uploadu oraz outputu na obcym urządzeniu. Udany test daje start/stop, erase i close; błąd startu oraz dwukrotne anulowanie również kończą cleanup przed zwolnieniem bramki.
- Tożsamość deskryptora odczytywana jest przez `EVIOCGID` i ośmiobajtowy `input_id`, zgodnie z [nagłówkiem UAPI Linuksa](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/input.h). Są to testy lokalne z symulowanymi ioctl, bez pomiarów na fizycznym urządzeniu.

### Weryfikacja końcowa

- Dodano **12 testów backendu** w [test_seventh_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_seventh_audit.py>) oraz **5 scenariuszy frontendu** w [frontend_battery.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_battery.cjs>).
- Backend: **549 testów, 531 zaliczonych, 18 pominiętych na Windows**, bez błędów. Wszystkie **11 zestawów frontendu zaliczone**.
- Strict TypeScript z unused/unreachable checks, build i walidator bundle: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Finalny ZIP: **85 plików, 14 katalogów, 490 661 bajtów**. Dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje i zgodność zawartości z checkoutem.
- SHA-256 ZIP: `3739eb996fd14ad23cee4d58f6119e597a14d238b9b4db8f2c6bb8c383947495`.
- Build odtworzono z rozpakowanego finalnego ZIP w oddzielnym katalogu po `npm ci --ignore-scripts` z dołączonego lockfile. Strict TypeScript, walidator bundle oraz payload check: zaliczone. `dist/index.js` i mapa źródeł są **identyczne bajtowo** z lokalnym buildem.
- `git diff --check`: zaliczony. HEAD pozostaje bez zmian; nie wykonano commita ani pusha.

Ponowny przegląd zmienionych ścieżek oraz ich otoczenia nie ujawnił dalszej potwierdzonej usterki wymagającej lokalnej poprawki. Fizyczne write/readback, Steam CEF oraz pominięte operacje Linux/POSIX pozostają poza wykonaną weryfikacją. Zdalnego CI nie uruchamiano.

Poniżej zachowano wcześniejsze audyty i ich ówczesne wyniki.

## Siódmy audyt i naprawy — zapis historyczny z 2026-10-10

Pełny lokalny audyt aktualnego kodu zakończono naprawą **6 potwierdzonych problemów H1–H6** dotyczących responsywności i dopuszczania operacji. Przejrzano wszystkie osiem modułów, lifecycle i recovery, współdzielenie zasobów, kolejki RPC i UI, widoczność stron, kontrolę OLED, aktualizacje, ustawienia, zależności oraz build i pakowanie. Kandydatów weryfikowano na rzeczywistych funkcjach i komponentach z kontrolowanym zakończeniem odczytów. Potwierdzone problemy naprawiano od razu, następnie ponownie sprawdzono zmienione ścieżki i ich integrację.

**Bez commita, pusha, instalacji i zapisów na konsoli.** Zachowano wcześniejsze lokalne poprawki oraz zakres funkcji Companion. HEAD pozostaje `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Problem i zastosowana naprawa | Kod |
| --- | --- | --- | --- |
| H1 | P2 | Publiczny odczyt stanu modułów czekał na wszystkie rozpoczęte RPC i bramkę zarządzania, także podczas długiego przywracania. Snapshot jest dostępny niezależnie od tej kolejki. Mutacje nadal czekają na wcześniejsze operacje; konflikt i shutdown nadal odrzucają odczyt. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:136>) |
| H2 | P2 | Monitor kontrolera synchronicznie pobierał ustawienia, których recovery lub blokada pliku mogły zatrzymać event loop. Odczyt działa teraz w wątku roboczym, tak jak podczas startu modułu. | [controller_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/controller_backend.py:858>) |
| H3 | P2 | Pierwszy krok suwaka szybkości RGB natychmiast rozpoczynał zapis i blokował następne kroki gestu. Szybkość ma debounce 450 ms, wersję callbacka, zapis końcowej wartości i flush przy zamknięciu lub innym działaniu. Polling respektuje oczekujący zapis. | [rgb.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx:313>) |
| H4 | P2 | Operacje power-save, wykrywanie WiFi, polecenia startowe i legacy restore wykonywały blokujące I/O lub subprocessy na event loop. Przeniesiono je do zadań roboczych. Blokada mutacji sieciowych pozostaje na pętli głównej i jest zwalniana dopiero po zakończeniu pracownika, także po wielokrotnym anulowaniu. | [wifi_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/wifi_backend.py:3076>) |
| H5 | P2 | Strona modułów dopuszczała spóźnione callbacki po zamknięciu oraz powtórzony restart Gaming Mode, także równolegle ze zmianą modułu. Wspólna bramka gestów sprawdza zamontowanie i zajętość; restart blokuje pozostałe działania, obsługuje błąd i pozwala ponowić próbę po zakończeniu. | [index.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/index.tsx:382>) |
| H6 | P2 | Callback ze zamkniętej strony About mógł rozpocząć nowe sprawdzenie lub pobieranie aktualizacji. Dopuszczanie wymaga teraz zamontowanej strony. Wcześniej przyjęte żądania nadal kończą się w sesji plugina, a ich wynik jest dostępny po ponownym otwarciu About. | [updates.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/updates.tsx:98>) |

### Dowody poprawy responsywności

- Celowo zatrzymany odczyt sprzętowy oraz przywracanie modułu nie zatrzymują publicznego snapshotu statusu. Test kontrolny potwierdza, że mutacja nadal czeka na wcześniejszy RPC.
- Celowo powolne ustawienia kontrolera i WiFi, wykrywanie urządzenia, startowy `systemctl` oraz legacy restore pozwalają heartbeatowi event loop wykonać się podczas oczekiwania. Przed poprawką heartbeat nie mógł się wykonać w tych ścieżkach.
- Dwa szybkie kroki Speed nie wykonują dwóch zapisów: końcowy gest powoduje jeden RPC. Suwak pozostaje dostępny podczas debounce; stary lub powtórzony callback nie dodaje pracy. Zamknięcie strony zapisuje zaakceptowany gest raz.
- Regresja z rzeczywistym plikiem ustawień i dziennikiem WiFi, przy symulowanych `nmcli`/`iw`, potwierdza udaną zmianę i przywrócenie wartości wyjściowych oraz rollback po błędzie. Polecenia wykonują się poza wątkiem event loop. Dwukrotne anulowanie nie wypuszcza następnej transakcji przed zakończeniem poprzedniej.

Są to lokalne dowody kolejności, liczby operacji i braku blokowania pętli. Nie wykonano pomiarów czasu na fizycznym Legion Go 2.

### Weryfikacja końcowa

- Dodano **11 testów backendu** w [test_sixth_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_sixth_audit.py>) i **9 scenariuszy frontendu**: 2 dla zarządzania modułami, 2 dla Speed, 3 zabezpieczające kolejność slider/action RGB i 2 dla callbacków aktualizacji.
- Backend: **537 testów, 519 zaliczonych, 18 pominiętych na Windows**, bez błędów. Wszystkie **11 zestawów frontendu zaliczone**, w tym wcześniejsze regresje TDP, OLED, wibracji, kontrolera i lifecycle.
- Strict TypeScript wraz z unused/unreachable checks, build, walidator bundle oraz payload check: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- ZIP: **85 plików, 14 katalogów, 489 871 bajtów**. Dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje oraz zgodność plików z checkoutem.
- SHA-256 ZIP: `8e5eb8e34ae53dfc762d6d296b7c3f54137a3d0bc44fa40594425fe784bddc18`.
- Rozpakowano finalny ZIP do oddzielnego katalogu. `npm ci --ignore-scripts` z dołączonego lockfile, strict TypeScript, build i payload check: zaliczone. `dist/index.js` oraz `dist/index.js.map` odtworzono **identycznie bajtowo**.
- `git diff --check`: zaliczony. Lokalny HEAD bez zmian; zdalnego CI nie uruchamiano.

Ponowny przegląd po H1–H6 nie wykazał dalszej potwierdzonej usterki wymagającej lokalnej poprawki. Testy nie obejmują rzeczywistego write/readback sprzętu, sesji Steam CEF ani pominiętych operacji Linux/POSIX.

Poniżej zachowano historię wcześniejszych audytów i ich ówczesne wyniki.

## Szósty audyt i naprawy — zapis historyczny z 2026-10-10

Kolejny pełny audyt aktualnego checkoutu zakończono naprawą **12 potwierdzonych problemów G1–G12**. Przegląd objął wszystkie osiem modułów, zarządzanie ich cyklem życia, ustawienia i recovery, kolejki RPC, polling i callbacki UI, kontrolę OLED, helper/updater, zależności, build oraz paczkę. Po pierwszych 11 naprawach ponowna kontrola ujawniła G12; poprawiono również tę lukę i jeszcze raz wykonano weryfikację.

Wszystkie zmiany pozostają lokalne. Zachowano wcześniejsze poprawki i zakres funkcji Companion. **Bez commita, pusha, instalacji i zapisów na konsoli**; HEAD: `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Problem i zastosowana naprawa | Kod |
| --- | --- | --- | --- |
| G1 | P2 | Skasowany timer EPP, który zdążył trafić do kolejki przeglądarki, zapisywał wcześniejszą wartość i kasował nowszy timer. Callback działa wyłącznie, gdy nadal jest bieżącym, niewykonanym gestem. Zachowano zapis przy zamknięciu. | [tdp.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:932>) |
| G2 | P2 | Świeże zabezpieczenie brightness writer/release kasowało cały cache obserwatora xprop. Kolejne odczyty uruchamiały procesy, a pojedyncze powiadomienie zostawiało niekompletny snapshot. `fresh=True` omija cache bez jego usuwania. | [display_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:577>) |
| G3 | P2 | UI WiFi odczekiwało dodatkowe trzy sekundy po zweryfikowanym reconnect. Usunięto timer; odczyt statusu rozpoczyna się od razu po zakończeniu operacji i wcześniejszego odczytu. | [wifi.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/wifi.tsx:132>) |
| G4 | P2 | Odpowiedź lub błąd mutacji WiFi aktualizowały odmontowaną stronę; zachowany callback mógł rozpocząć kolejną operację. Dodano kontrolę mounted/visible przy przyjęciu operacji i mounted przy odpowiedzi. | [wifi.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/wifi.tsx:132>) |
| G5 | P2 | Ponowne otwarcie podczas oczekującego odczytu odrzucało starą odpowiedź, ale nie uruchamiało świeżej. Display, RGB, Remap, WiFi, Battery i Controller teraz wykonują jeden odczyt po zakończeniu poprzedniego. Polling nadal nie nakłada RPC. | [display.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/display.tsx:152>), [battery.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/battery.tsx:123>), [controller.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/controller.tsx:155>) |
| G6 | P2 | Callbacki Display, Remap, RGB i Vibration po odmontowaniu mogły tworzyć nowy stan i nowe zapisy sprzętowe. Odrzucane są nowe gesty zamkniętej strony; Remap i RGB nadal kończą gesty przyjęte przed zamknięciem. | [display.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/display.tsx:181>), [remap.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/remap.tsx:133>), [rgb.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx:224>) |
| G7 | P2 | Status modułów, wersja i zdarzenia guard wykonywały synchroniczny odczyt metadanych/sesji na event loop. Odczyty i zapis identyfikatora wycofanej sesji przeniesiono do wątku. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:294>) |
| G8 | P2 | Atomowe commity wyboru modułów, wraz z fsync i backupem, blokowały wszystkie zadania backendu. Są wykonywane w wątku, z oczekiwaniem na pełne zakończenie także po anulowaniu wywołania. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:301>) |
| G9 | P2 | Dwa szybkie kliknięcia Test Vibration wysyłały dwa RPC, a zakończenie testu aktualizowało zamkniętą stronę. Synchroniczny ref coalescuje kliknięcia; odpowiedź respektuje mounted. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:680>) |
| G10 | P2 | Test wibracji uruchomiony przed debounced zapisem suwaka używał wcześniejszej intensywności. Test flushuje przyjęte edycje i czeka na ich zakończenie. Zamknięcie strony lub zmiana gry podczas oczekiwania odrzuca opóźniony test. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:680>) |
| G11 | P2 | Reset wibracji zapisywał wartości domyślne przed zastosowaniem sprzętowym, ale przy błędzie sprzętu UI zachowywało poprzednie ustawienia. Teraz przyjmuje autorytatywny snapshot zapisanego profilu również z odpowiedzi `success=false`, zachowując komunikat błędu. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:652>) |
| G12 | P2 | Hybrid, korekcja HDR, reset i uninstall mogły przyjąć snapshot sprzed docka lub restartu gamescope i ustawić wewnętrzny tryb/baseline na innym wyjściu. Dodano świeżą kontrolę connector/content oraz ponowną kontrolę sesji przed tymi zapisami. Nieznana sesja/wyjście odracza zmianę. | [display_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1663>), [korekcja HDR](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:2296>), [Hybrid](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:2375>) |

### Responsywność i dowody

Regresje wykonują rzeczywisty TSX z kontrolowaną kolejnością odpowiedzi i timerów. Odtworzono zapis starego EPP, brak świeżego odczytu po ponownym otwarciu, zapisy po odmontowaniu, dodatkowy timer 3000 ms, dwa testy wibracji i reset pozostawiający stary UI. Backend używa rzeczywistych atomowych plików ustawień oraz atrap procesów i sprzętu. Nowe testy OLED odtworzyły odczyt cache sprzed docka i próbę ustawienia baseline/HDR na wyjściu zewnętrznym.

- WiFi usuwa **3000 ms stałego oczekiwania UI** po operacji backendu. Nie skraca to samego skanu, reconnect ani weryfikacji sieci.
- Szybkie ponowne otwarcie uruchamia świeży odczyt po zakończeniu poprzedniego, bez czekania na następny interwał 1 s lub 10 s. Seria otwarć nadal ma najwyżej jeden oczekujący odczyt i jeden późniejszy świeży odczyt.
- Próba brightness write → powiadomienie obserwatora → odczyt gate wymaga **jednego odczytu uncached zamiast dwóch** i zachowuje pełny snapshot. Świeża kontrola nadal wykrywa dock oraz zmianę wartości przez Steam.
- Stabilny Hybrid tick korzysta z cache i **nie wykonuje odczytu uncached ani zapisu sprzętowego**. Baseline, reklamowanie HDR, możliwa zmiana trybu oraz hand-back/reset/uninstall wymagają świeżej kontroli. To celowy koszt bezpiecznej zmiany wyjścia, nie koszt każdego stabilnego ticka.
- Wstrzymany odczyt sesji i commit ustawień pozwalają heartbeatowi event loop wykonać się podczas oczekiwania. Dwukrotne anulowanie zmiany modułu nie kończy jej przed durable commitem i wycofaniem sprzętu.

To dowody kolejności i liczby operacji z testów lokalnych; nie są pomiarem opóźnień na fizycznym Legion Go 2.

### Weryfikacja końcowa

- Dodano **17 testów backendu** w [test_fifth_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_fifth_audit.py>) i **22 scenariusze frontendu**: 2 EPP, 13 polling/lifecycle/WiFi/RGB oraz 7 wibracji. Atrapy OLED uwzględniają jawny świeży odczyt.
- Backend: **526 testów, 508 zaliczonych, 18 pominiętych na Windows**, bez błędów. Frontend: **wszystkie 11 zestawów zaliczone**, w tym 29 scenariuszy TDP recovery.
- Strict TypeScript, unused/unreachable checks, build, walidator bundle i payload check: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Finalny ZIP: **85 plików, 14 katalogów, 489 144 bajty**. Dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje i zgodność plików z checkoutem.
- SHA-256 ZIP: `731a1caa7b262c269f0c31bda73761b05d4f9eb963abfa944f7d03bd35337288`.
- Świeża instalacja zależności z lockfile w katalogu rozpakowanej finalnej paczki, strict TypeScript, build i payload check: zaliczone. Build odtworzono poza checkoutem, bez lifecycle scripts zależności. `dist/index.js` i source map są **identyczne bajtowo** z lokalnym buildem.
- `git diff --check`: zaliczony. HEAD pozostaje bez zmian; nie wykonano commita ani pusha.

Ponowny przegląd po G12 nie wykazał dalszych potwierdzonych usterek wymagających lokalnej poprawki. Fizyczne write/readback, sesja Steam CEF i Linux/POSIX pozostają poza wykonaną weryfikacją. Nie uruchamiano zdalnego CI.

Poniższe sekcje dokumentują wcześniejsze audyty i ich ówczesne wyniki.

## Piąty audyt i naprawy — zapis historyczny z 2026-10-10

Pełny lokalny przegląd jakości i responsywności zakończono naprawą **9 potwierdzonych problemów**: pięciu otwartych E1–E5 oraz czterech dodatkowych F1–F4. Zachowano wcześniejsze poprawki A–D i zakres funkcji Companion. Nie wykonano commita, pusha, instalacji ani zapisów na konsoli; HEAD pozostaje `61bad19bde08d937c4678ddc73c3367401b0a0ec`.

| ID | Priorytet | Naprawa i wynik | Kod |
| --- | --- | --- | --- |
| E1 | P2 | Odpowiedź heartbeat ma epokę odczytu. Edycja, rozpoczęcie i zakończenie mutacji unieważniają starsze snapshoty. Zmiana gry podczas zajętego RPC jest wysyłana od razu po jego zakończeniu. | [vibration.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:308>) |
| E2 | P2 | Globalny kontekst staje się gotowy dopiero po przyjęciu aktualnej odpowiedzi; anulowany odczyt wymusza nowy. Intencja odtworzenia globalnych limitów przetrwa zmianę dostępności TDP. | [tdp.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1447>) |
| E3 | P2 | Release odczytuje aktualny output i atom z pominięciem cache, zachowuje zmienioną jasność monitora i ponownie sprawdza sesję. Forward również odrzuca gate sprzed dock/HDR switch. | [display_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1939>) |
| E4 | P2 | Cleanup po błędzie resume/startu zawsze czeka na wątki, również gdy unload zawiedzie. Zwykłe wyłączenie pluginu kończy także wcześniej anulowane prace monitorów. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:238>) |
| E5 | P2 | Oddzielono błąd odczytu profilu od błędu zastosowania TDP. Świeży profil CPU pozostaje dostępny po błędzie apply; Retry Extras działa także przy niedostępnym profilu. Ochrona TDP pozostaje aktywna do zakończenia auto-apply. | [tdp.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1490>) |
| F1 | P2 | Stary callback RGB nie może zapisać zastąpionego koloru ani powtórzyć gestu. Odpowiedzi zachowują nowsze drafty poszczególnych pól; błędny stary draft nie wraca przy następnym potwierdzonym zapisie. | [rgb.tsx](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx:228>) |
| F2 | P2 | Zastąpiono globalną serializację wszystkich RPC blokadami zasobów. Wolne WiFi nie opóźnia RGB ani Battery. Zarządzanie modułami, konflikt i shutdown nadal czekają na wszystkie dopuszczone RPC. Controller/Remap oraz odczyt baterii kontrolera współdzielą odpowiednią blokadę. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:226>) |
| F3 | P3 | Równoległe RPC współdzielą tylko trwający skan konfliktów. Ukończony skan nie jest cache'owany, również po anulowaniu jedynego oczekującego klienta. | [main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:407>) |
| F4 | P2 | Anulowanie drain nie anuluje tasków opakowujących to_thread i nie ukrywa nadal pracujących wątków. Żądanie anulowania jest propagowane dopiero po zakończeniu ich pracy. | [module_runtime.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/module_runtime.py:37>) |

### Potwierdzenie nowych usterek i responsywności

Przed naprawą F1 wykonanie wyczyszczonego callbacku zapisywało hue=120 zamiast nowszego hue=240. Starsza odpowiedź RGB mogła również zastąpić nowszy draft innego suwaka. Regresje sprawdzają eliminację zbędnego zapisu, zachowanie bieżącego draftu oraz poprawne odtworzenie wartości po błędzie transportu.

Przed naprawą F2 zatrzymany odczyt WiFi blokował niezależny odczyt RGB przez co najmniej 500 ms testowego deadline. Po zmianie RGB kończy się bez zwalniania WiFi. Testy sprawdzają również serializację tego samego zasobu, współdzielone operacje InputPlumber, oczekiwanie disable na dopuszczone żądania, odrzucenie nowych żądań po wyłączeniu i bezpieczne zatrzymanie anulowanego RPC.

Kontrolowany benchmark trzech niezależnych odczytów, każdy ze sztucznym opóźnieniem 150 ms, dał medianę **451,4 ms** przy poprzedniej globalnej serializacji i **150,4 ms** przy nowych blokadach modułów. Każdy wariant wykonano trzy razy. To wynik lokalnych atrap i porównania poprzedniego mechanizmu blokady z bieżącym kodem, a nie pomiar przyspieszenia na fizycznym Legion Go 2. [Surowy wynik](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_responsiveness_benchmark.json>).

Przed naprawą F3 trzy równoległe żądania uruchamiały trzy skany tych samych zainstalowanych pluginów. Po zmianie korzystają z jednego trwającego skanu. Późniejsze żądania nadal wykonują świeżą kontrolę, więc ta optymalizacja nie wydłuża czasu wykrywania konfliktu.

Przed naprawą F4 anulowany drain kończył się przed fizycznym wątkiem, a anulowany task tego wątku znikał z rejestru. Regresja utrzymuje pracę w kontrolowanym eventcie i potwierdza, że drain pozostaje oczekujący aż do rzeczywistego zakończenia. Dodatkowa próba E4 wykazała tę samą potrzebę oczekiwania przy zwykłym shutdown, co również poprawiono.

Ochronę OLED rozszerzono na opóźniony backlight event po docku, pojawieniu się HDR content i zmianie output feedback. Nieznany output lub nieprawidłowa wartość atomu odracza release. Świeży odczyt przed zapisem wymaga dodatkowego odczytu xprop; to celowy koszt weryfikacji ownership, ograniczony do aktywnego legacy writer/release.

### Testy i paczka końcowa

- Dodano **16 testów backendu** w [test_fourth_audit.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_fourth_audit.py>) i **12 scenariuszy frontendu**: 4 wibracji, 5 TDP i 3 RGB. Dwie wcześniejsze atrapy OLED uzupełniono o aktualizację live atomu zgodną z udanym zapisem.
- Backend: **509 testów, 491 zaliczonych, 18 pominiętych na Windows**, bez błędów.
- Frontend: **wszystkie 11 zestawów zaliczone**, w tym 29 scenariuszy TDP recovery.
- Strict TypeScript, unused/unreachable checks, build i walidator bundle: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Payload check: **40 pozycji**. Nowy ZIP: **85 plików, 14 katalogów, 486 775 bajtów**. Dokładny walidator z CI potwierdził CRC, bezpieczne ścieżki, kompletność, licencje i zgodność plików z bieżącymi źródłami.
- SHA-256 ZIP: `3c07830c1e1b12f2f0f3a219be1c53b2c6a12cf83ca53049c2ff7bee041f7d2c`.
- Świeży katalog z rozpakowanego ZIP: instalacja zależności z lockfile z wyłączonymi lifecycle scripts, strict TypeScript, build i payload check zaliczone. `dist/index.js` i source map są **identyczne bajtowo** z lokalnym buildem.
- `git diff --check`: zaliczony. HEAD bez zmian; bez commita i pusha.

Końcowy przegląd po naprawach objął wszystkie moduły, kolejki i polling frontendu, gotowość i scope profili, błędy zapisu, lifecycle i anulowanie pracy, legacy/native OLED, odtwarzanie ustawień, helper/updater, zależności oraz zawartość paczki. **Nie pozostały dalsze potwierdzone problemy do naprawy w tym lokalnym przeglądzie.** Nie wykonywano fizycznych write/readback na Legion Go 2, testów Steam CEF ani zdalnego CI.

Poniższe sekcje są zapisem wcześniejszych audytów. E1–E5 są zamknięte przez powyższe poprawki.

## Czwarta weryfikacja — historyczny zapis 5 dalszych problemów

Ponowny audyt po C1–C7 i D1–D2 potwierdził **5 dalszych problemów P2**. E2 i E5 ujawniają regresje nowego gate gotowości profilu; E1, E3 i E4 dotyczą sąsiednich ścieżek, których dotychczasowe regresje nie obejmowały. Odtworzono je na bieżących źródłach, przy użyciu realnego TSX, atomowych plików ustawień lub rzeczywistego mechanizmu offload/unload z atrapą sprzętu.

To sesja weryfikacyjna: nie zmieniano kodu działania ani obowiązkowych testów. Reprodukcje zapisano poza checkoutem. HEAD pozostaje `61bad19bde08d937c4678ddc73c3367401b0a0ec`; bez commita, pusha, instalacji i zapisów na konsoli. Opisy napraw i wcześniejszego czystego przeglądu poniżej dokumentują poprzednią sesję, nie aktualny brak otwartych usterek.

| ID | Priorytet | Potwierdzony problem | Miejsce |
| --- | --- | --- | --- |
| E1 | P2 | Heartbeat rozpoczęty przed mutacją wibracji przywraca stary scope już po udanym commicie | [przyjęcie odpowiedzi](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:475>), [publikacja heartbeat](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:316>) |
| E2 | P2 | Zmiana dostępności TDP przed zakończeniem odczytu globalnego przedwcześnie otwiera edytor i unieważnia świeżą odpowiedź | [pominięcie odczytu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1486>), [przedwczesny znacznik gotowości](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1490>) |
| E3 | P2 | Disable OLED po podłączeniu monitora może przywrócić wewnętrzny baseline na zewnętrznym wyjściu tej samej sesji | [release brightness](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1936>) |
| E4 | P2 | Błąd restore_intent przy włączaniu modułu kończy cleanup bez drain oczekującego wątku sprzętowego | [cleanup po błędzie resume](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:304>) |
| E5 | P2 | Błąd auto-apply TDP blokuje Retry Extras i ukrywa niezależne CPU controls mimo odczytanego profilu | [gate wszystkich operacji](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1159>), [CPU](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:2077>), [Retry Extras](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:2098>) |

### E1 Spóźniony heartbeat po udanym toggle wibracji

Gra 111 korzysta z globalnego profilu. Heartbeat pobiera snapshot `profile_id=0, overwrite=false`, ale jego odpowiedź zostaje opóźniona. Następnie użytkownik włącza Per Game i otrzymuje udane potwierdzenie `profile_id=111, overwrite=true`. Gdy stary heartbeat dociera po zakończeniu mutacji, `mutationsPending=0` i brak pendingWrites pozwalają `adoptResponse` przyjąć go jako aktualny: checkbox ponownie pokazuje Off. Następny gest wysyła oczekiwany profil 0 i jest odrzucany przez backend, którego trwały profil jest już 111. Analogiczna stara odpowiedź może cofnąć pokazywane wartości suwaków.

Nowa blokada gestów podczas toggle nie chroni odczytu rozpoczętego **przed** mutacją i zakończonego **po** niej. Potrzebna jest generacja/epoka odczytu i mutacji lub równoważny znacznik świeżości snapshotu, z zachowaniem pierwszeństwa rzeczywistej zmiany gry. [Reprodukcja TSX](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_vibration_heartbeat.cjs>) przeszła; osobna próba z rzeczywistym AtomicSettingsManager potwierdziła trwały overwrite=true, odmowę settera oczekującego profilu 0 i brak zapisu do sprzętu przy tej odmowie.

### E2 Przedwczesne noGameSynced

Przy wyjściu z gry znacznik `noGameSyncedRef=true` jest ustawiany przed zakończeniem `getSettings()`. Jeżeli w tym czasie polling caps zmieni `tdpAvailable`, efekt unieważnia trwający odczyt i rozpoczyna nową generację. Nowa generacja widzi ustawiony znacznik, wywołuje `complete()` i pomija świeży odczyt. Po powrocie dostępności TDP Apply jest aktywne ze starym globalnym snapshotem. W próbie wcześniejsze ustawienia wynosiły 15 W, a oczekująca świeża odpowiedź 31 W; jej późniejsze dostarczenie jest ignorowane, zaś Apply wysyła 15 W.

To luka w C1, nie powtórzenie zapisu wartości poprzedniej gry do innego appId: edytor globalny zostaje uznany za gotowy bez przyjęcia jego aktualnych ustawień. Znacznik powinien oznaczać zakończony, nadal aktualny snapshot, a unieważnienie odczytu powinno wymusić nowy. [Reprodukcja TSX](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_tdp_global_read.cjs>) potwierdza cały ciąg: game exit → pending global read → caps unavailable/available → odrzucona odpowiedź 31 W → Apply 15 W.

### E3 Wycofanie jasności po zmianie wyjścia w tej samej sesji

Legacy PQ posiada baseline 250 nitów i ostatni zapis 120. Po podłączeniu monitora nowy atom ma 700 nitów i `IS_EXTERNAL=1`, ale compositor nadal ma tę samą sesję 100. Wywołanie publicznego `set_enabled(False)` przed następnym gate przechodzi guard sesji i zapisuje 250, nadpisując wartość zewnętrznego wyjścia. Gdyby `_refresh_gate` zdążył wcześniej, istniejąca kontrola zmienionego live value porzuciłaby ownership i zachowała 700.

Sprawdzenie sesji z C3/D1 jest potrzebne, ale nie rozstrzyga bieżącego wyjścia ani tego, czy atom nadal zawiera naszą wartość. Release powinien bezpośrednio potwierdzać te warunki, zamiast polegać na wcześniejszym gate. [Reprodukcja z publicznym setterem](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_display_release.py>) potwierdza zapis 250 oraz porównawczą ścieżkę gate, która pozostawia 700. Cały dostęp do atomów jest atrapą.

### E4 Brak drain po nieudanym resume modułu

Ścieżka enabled=true może poprawnie uruchomić `_main`, a następnie otrzymać błąd `restore_intent`, np. przy odtwarzaniu RGB/power LED. Catch zapisuje disabled/pending i wywołuje `_unload`, lecz nie wykonuje `module_runtime.drain`. Anulowanie taska monitora RGB nie anuluje jego wątku offload. Wywołanie modułu kończy się błędem i zwalnia gate, podczas gdy wątek sprzętowy nadal pracuje; może zakończyć zapis już po zgłoszeniu disabled.

[Reprodukcja](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_module_enable_failure.py>) używa publicznego modules_set_enabled, rzeczywistego RGB `_unload` oraz module_runtime.offload. Zatrzymana atrapa sprzętu pozostaje w `_workers` po powrocie błędu i kończy pracę dopiero po ręcznym zwolnieniu jej testowego eventu. Cleanup błędnej próby resume powinien zakończyć również oczekujące wątki, z ochroną drain także wtedy, gdy sam unload się nie powiedzie.

### E5 Gate profilu odcina ścieżkę naprawy helpera i CPU

Profil gry zostaje odczytany poprawnie. Caps wskazują dostępną ścieżkę firmware, niedostępny helper i oferują retry. Auto-apply może jednak otrzymać odmowę firmware, a fallback RyzenAdj jest niedostępny. Efekt ustawia profileError i pozostawia profileReadyRef pusty. Ten stan wyłącza Retry Extras, a nawet bezpośrednie wywołanie jego handlera kończy się w `beginOperation(false)` bez RPC. Równocześnie znika CPU Power Controls, mimo że jego dostępność nie wynika z powodzenia Apply TDP.

Backend dopuszcza fallback z nieudanego WMI do RyzenAdj (`tdp_backend.py:1022–1039`), więc brak dostępu do download retry może blokować właśnie potrzebną naprawę. Gotowość odczytanego kontekstu, wynik zastosowania TDP i dopuszczenie niezależnego helper retry powinny być rozdzielone. [Reprodukcja TSX](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/fourth_tdp_recovery_gate.cjs>) potwierdza disabled=True, zero wywołań retry_extras po próbie handlera oraz brak CPU section.

### Powtórzona weryfikacja

- Backend: **493 testy, 475 zaliczonych i 18 pominiętych na Windows**, bez błędów.
- Frontend: **wszystkie 11 zestawów zaliczone**, w tym 24 scenariusze TDP recovery.
- Strict TypeScript, unused/unreachable checks: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Ponowny build i walidacja bundle: zaliczone. Payload check: **40 pozycji**.
- Dokładny walidator ZIP z CI: CRC, ścieżki, kompletność, licencje i zgodność z bieżącymi plikami zaliczone. ZIP pozostał bez zmian: **483 268 bajtów**, SHA-256 `4122edcd504780a2daf0ca71381550e1f9853d27ce7150d79de2ae7cc23f65ef`.
- `git diff --check`: zaliczony. Reprodukcje E1–E5 uruchomiono poza checkoutem i wszystkie potwierdziły opisane zachowanie.

Przejście standardowych testów i poprawne pakowanie nie zamykają E1–E5. Nie wykonywano testów fizycznego zapisu/readback na Legion Go 2, Steam CEF ani zdalnego CI. W tej rundzie otwarte problemy pozostawiono do naprawy; zmieniono jedynie raport.

## Poprzednia sesja — status napraw C1–C7 i D1–D2

Wszystkie **7 zgłoszonych problemów C1–C7 naprawiono lokalnie**. Pierwsza pełna kontrola po naprawach przeszła 489 testów backendu, 11 zestawów frontendu, strict TypeScript, build, payload check i walidację nowego ZIP. Następny przegląd wykrył jeszcze **2 problemy P2, D1–D2**, odtworzone nowymi testami przed zmianą kodu i następnie naprawione. Zachowano wcześniejsze poprawki A1–A8 i B1–B9. HEAD pozostaje `61bad19bde08d937c4678ddc73c3367401b0a0ec`; bez commita, pusha, instalacji i zapisów na urządzeniu.

| ID | Wprowadzona naprawa | Regresje |
| --- | --- | --- |
| C1 | Edytor wycofuje stary kontekst gry i odmawia zapisu przed świeżym odczytem; brak profilu ładuje aktualne ustawienia globalne, a błąd udostępnia Retry. | Pending profil, stary handler Apply, brak profilu, błąd odczytu/global settings, retry, A→B→A i unmount |
| C2 | Podmiana i przywracanie obejmują pełną wartość nazwy YAML wraz z liniami kontynuacji. | Literal, folded i quoted multiline name; zachowanie wielowierszowego description i baseline w cyklu enable→repair→disable |
| C3 | Release sprawdza bieżącą sesję gamescope; nowa sesja porzuca stare ownership, nieznana odracza cleanup bez zapisu. | Publiczny disable i unload po restarcie, nieznana sesja oraz skuteczny retry tej samej sesji |
| C4 | Zmiana profilu/reset blokują nowe gesty; odpowiedzi przyjmują pełny kontekst profilu, a niejednoznaczny błąd transportu wymaga potwierdzenia ustawień lub Retry. | Toggle plus gest, odrzucony commit, commit plus hardware failure, zmieniony scope w odpowiedzi settera, niedostępny resync |
| C5 | Odczyt profilu i wybór istniejącego Battery/AC dla CPU działają niezależnie od dostępności TDP. | Brak firmware/helper: zapisany AC scope trafia do CPU controls bez wywołań Apply TDP lub Set Game AC Profile |
| C6 | Nowy profil ponad limit jest odrzucany przed commitem/hardware; loader zachowuje wcześniejsze większe kolekcje. | 512 wpisów, edycja/usunięcie istniejącego, zwolnienie miejsca, zachowanie 513 wpisów po niezwiązanej edycji |
| C7 | Wspólny join rozdziela również odtwarzane bloki bez końcowego LF. | Powrót do F7/F8 i zachowanie obcego mapowania za ostatnim blokiem |
| D1 | Stary gate jasności i szybki writer Hybrid również weryfikują sesję przed zapisem; do czasu przyjęcia nowej sesji nie zmieniają jej atomów. | Dwa testy najpierw potwierdziły zapis jasności/HDR po restarcie; po poprawce żadna ścieżka nie zapisuje atomów |
| D2 | Utworzenie profilu wibracji kopiuje wartości z tego samego jednorazowego snapshotu gry co guard/odpowiedź. | Fallback `/proc` 222→111 podczas tworzenia: jeden odczyt, kopia profilu 222 |

Dodano **14 testów backendu** w `tests/test_third_audit.py` oraz **10 scenariuszy frontendu** w istniejących zestawach recovery. Wszystkie testy urządzeń używają atrap i plików tymczasowych. Szczegółowe wyniki końcowe znajdują się w sekcji poniżej.

### Weryfikacja końcowa poprzedniej sesji naprawczej

- Backend: **493 testy, 475 zaliczonych i 18 pominiętych na Windows**, bez błędów. Pominięcia dotyczą wymagań POSIX; nie uruchamiano zdalnego CI.
- Frontend: **wszystkie 11 zestawów zaliczone**, w tym 24 scenariusze TDP recovery i rozszerzone regresje wibracji.
- Strict TypeScript, unused/unreachable checks: zaliczone. `npm audit --audit-level=low`: **0 podatności**.
- Build i walidator wygenerowanego bundle: zaliczone. Payload check: **40 pozycji**.
- ZIP: **85 plików, 14 katalogów, 483 268 bajtów**; dokładny walidator z CI potwierdził CRC, ścieżki, kompletność, licencje oraz zgodność plików z bieżącymi źródłami.
- SHA-256 ZIP: `4122edcd504780a2daf0ca71381550e1f9853d27ce7150d79de2ae7cc23f65ef`.
- Świeży katalog z rozpakowanego ZIP: `npm ci --ignore-scripts --no-audit --no-fund`, strict TypeScript, build i payload check zaliczone. Odtworzone `dist/index.js` i source map są **identyczne bajtowo** z lokalnym buildem. Paczka nadaje się do odbudowy bez repozytorium Git i katalogu tests.
- Dodatkowa lokalna próba z pełnym parserem YAML i kanonizującym providerem potwierdziła enable→repair→disable oraz zachowanie nazwy, description i mapowań. Parser nie został dodany do zależności plugina ani obowiązkowych testów.
- `git diff --check`: zaliczony; HEAD niezmieniony.

Cykl zakończono ponownym przeglądem po naprawach D1–D2: sprawdzono writer/release OLED, odczyty i mutacje profili TDP/CPU/wibracji, kolejki i spóźnione odpowiedzi frontendu, YAML restore, lifecycle modułów, pomocniki procesów/downloadów oraz build/pakowanie. **Nie znaleziono dalszych potwierdzonych usterek ani regresji w końcowym lokalnym przeglądzie.** Nie oznacza to dowodu braku wszystkich możliwych błędów. Testy fizycznego zapisu/readback na Legion Go 2 oraz Steam CEF pozostają niewykonane. Poniższy opis C1–C7 jest historycznym zapisem usterek sprzed napraw, a nie listą otwartych problemów.

## Trzecia weryfikacja — historyczny zapis 7 problemów

Ponowny przegląd po drugiej iteracji poprawek potwierdził **7 dalszych problemów: 6 P2 i jeden P3**. Ochrona spóźnionych odpowiedzi B4 nie obejmuje gotowości edytora po zmianie gry ani rollbacku równoległych operacji w tej samej grze. Rozdzielenie dostępności TDP/CPU z B9 pozostaje niepełne dla osobnego profilu AC. W remaperze pozostaje rzadka krawędź przywracania bloków oraz odrębna usterka obsługi poprawnej wielowierszowej nazwy profilu.

Zakres: aktualne niecommitowane źródła na bazie `61bad19bde08d937c4678ddc73c3367401b0a0ec`. Nie zmieniano w tej weryfikacji kodu działania ani istniejących testów. Reprodukcje zapisano poza checkoutem, a poniższe wyniki powtórzono niezależnie od standardowego zestawu. Nie wykonywano pusha ani zapisów na konsoli.

| ID | Priorytet | Problem | Miejsce |
| --- | --- | --- | --- |
| C1 | P2 | TDP pozwala zapisać wartości poprzedniej gry do nowej przed odczytem jej profilu | [odczyt nowego profilu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1471>), [Apply](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:2000>) |
| C2 | P2 | Wielowierszowa nazwa poprawnego profilu remapera prowadzi do utraty oryginalnego baseline | [podmiana nazwy](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:417>), [przechwycenie baseline](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:500>) |
| C3 | P2 | Legacy OLED przywraca baseline poprzedniej sesji do nowego gamescope | [jasność](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1930>), [HDR](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:2510>) |
| C4 | P2 | Ruch suwaka wibracji blokuje rollback nieudanego włączenia profilu w tej samej grze | [guard toggle](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:673>), [odpowiedź pola](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:592>) |
| C5 | P2 | Brak TDP odcina edycję zapisanych ustawień CPU dla AC | [pominięty odczyt](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1412>), [blokada AC](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1905>) |
| C6 | P2 | Limit profili wibracji pozwala na pozornie udany zapis, a potem cicho usuwa nowy profil | [obcięcie listy](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:414>), [utworzenie](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:980>) |
| C7 | P3 | Przywracanie remapera nie rozdziela bloków baseline bez końcowego LF | [łączenie przy restore](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:556>) |

### C1 Zapis przed gotowością profilu nowej gry

Próba: gra 111 ma Custom 11/14/19 W, gra 222 ma zapisane 22 W. Po zmianie gry, podczas oczekiwania na `get_game_profile(222)`, ekran pokazuje nadal 11 W i aktywny przycisk „Apply & Save for Game 222”. Kliknięcie wysyła `11000/14000/19000` dla app 222 z oczekiwanym app 222, więc backendowy guard gry nie odrzuci tych wartości. Następnie prawidłowa odpowiedź profilu 222 jest odrzucona, bo operacja Apply unieważniła jej generację. Również zakończony odczyt `exists=false` pozostawia profil 111 i włączone Per Game zamiast wartości globalnych.

Nie jest to powtórzenie spóźnionej mutacji z B4: wartości poprzedniej gry są dostępne do zapisu, zanim nadejdzie pierwsza świeża odpowiedź nowej gry. Edytor powinien od razu wycofać poprzedni kontekst, blokować zapis do czasu odczytu, a brak profilu rozstrzygać przez aktualne ustawienia globalne. Reprodukcja: [third_frontend_tdp.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_frontend_tdp.cjs>), przypadki 1–2.

### C2 Utrata baseline przy wielowierszowej nazwie

Poprawna nazwa `User\nProfile\n` jest serializowana jako blok YAML. `_build_profile` zastępuje wiersz `name`, pozostawiając dalsze linie jego wartości. Serde interpretuje wynik jako nazwę `Legion Go 2 Companion Buttons User Profile`. Mapowania F1/F2 zostają załadowane, ale weryfikacja zgłasza błąd, bo nazwa różni się od oczekiwanej. Kolejna naprawa watchdoga uznaje ten profil za obcy i nadpisuje oryginalny baseline już zmienionymi mapowaniami. Disable zwraca sukces, lecz pozostawia F1/F2 zamiast oryginalnych F7/F8.

Kontrakt sprawdzono dla InputPlumber v0.78.0 wskazanego przez `SOURCE.md`: [nazwa jest typu String i przechodzi deserializację YAML](https://github.com/ShadowBlip/InputPlumber/blob/v0.78.0/src/config/mod.rs#L32-L48), a [GetProfileYaml używa serde_yaml](https://github.com/ShadowBlip/InputPlumber/blob/v0.78.0/src/dbus/interface/composite_device.rs#L126-L140). Reprodukcja używa rzeczywistego `serde_yaml 0.9.34` z Rust Playground do normalizacji generycznej struktury oraz atrap D-Bus; nie uruchamia InputPlumber na urządzeniu. Należy zastępować całą wartość nazwy YAML i nie przejmować nowego baseline z wyniku własnej nieudanej operacji. [third_lifecycle_remap_multiline_name.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_lifecycle_remap_multiline_name.py>) potwierdza cały ciąg enable → repair → disable.

### C3 Przywracanie OLED po zmianie sesji

Dotyczy legacy OLED bez natywnego profilu gamescope. Po zmianie sesji 100→200, przed kolejnym odświeżeniem setup, `set_enabled(False)` zapisuje stary baseline 250 nitów na świeżym atomie 700 nitów nowej sesji. Analogicznie unload Hybrid zapisuje stary HDR=False do nowej sesji z HDR=1. Native probe widzi nową sesję, lecz przy braku native support wraca bez synchronizacji legacy ownership; procedury release nie sprawdzają sesji baseline.

Trwałość zapisu naprawiona w B1 nie dowodzi własności wartości w nowej sesji. Release powinien porównywać aktualną sesję z sesją baseline i porzucać ownership starej sesji. Obie próby z publicznego setter/unload są w [third_hardware_repro.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_hardware_repro.py>).

### C4 Nieudany toggle i nowszy gest wibracji

Próba: użytkownik włącza profil gry 111, a następnie rusza nadal aktywny suwak, zanim toggle odpowie. Błąd zapisu przed commitem odrzuca RPC toggle. Nowszy `editSeq` powoduje pominięcie rollbacku, więc Per Game pozostaje włączone. Oczekujący setter poprawnie dostaje odmowę backendu z `profile_id=0, overwrite=false`, lecz frontend przyjmuje wyłącznie `settings`. Kolejne gesty nadal oczekują nieistniejącego profilu 111 i są odrzucane. Późniejszy idle heartbeat może odzyskać właściwy checkbox; nie jest to trwała blokada każdej sesji.

Kontrakt odmowy potwierdzono także z rzeczywistym `AtomicSettingsManager` i awarią zapisu pliku podstawowego: toggle odrzuca RPC, zapis pozostaje globalny, a setter zwraca oczekiwany pełny payload. Stan profilu powinien być uzgadniany niezależnie od wersji gestu pola; nowe pole nie może anulować koniecznego rollbacku w tym samym kontekście gry. Dowody: [frontend](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_frontend_vibration.cjs>) i [backend](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_root_vibration_contract.py>).

### C5 Niedostępne TDP blokuje zakres AC CPU

Przy `tdp_available=false`, sprawnych kontrolkach CPU i zapisanym osobnym profilu AC efekt kończy się przed `get_game_profile`. Wybór Per Game ustawia jedynie checkbox: zapisane `ac_separate=true` pozostaje nieznane, Separate AC jest wyłączone i zablokowane, a komponent CPU otrzymuje `acProfile=false`. Nie ma drogi do edycji aktywnych ustawień AC CPU na zasilaczu. B9 zachowuje edycję global/game CPU, ale nie pełny zakres battery/AC.

Odczyt metadanych profilu i wybór zakresu CPU powinny działać niezależnie od możliwości zapisu TDP. Reprodukcja: [third_frontend_tdp.cjs](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_frontend_tdp.cjs>), przypadek 3.

### C6 Przekroczenie limitu profili wibracji

Przy 512 profilach wraz z globalnym utworzenie kolejnego zwraca sukces i zapisuje 513 wpisów. Loader zwraca tylko pierwsze 512, więc nowy profil nie pojawia się w odczycie. Następna niezwiązana edycja globalnego profilu zapisuje obciętą kolekcję i usuwa nowy wpis również z dysku. Należy odrzucić utworzenie ponad limit przed zapisem lub jawnie zarządzać pojemnością, zachowując pozostałe dane. Próba z rzeczywistym plikiem ustawień: [third_hardware_repro.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_hardware_repro.py>).

### C7 Separator w rzadkiej ścieżce restore

Wymaga poprawnego baseline bez końcowego LF w zapisanym pliku, np. po edycji lub imporcie, oraz późniejszego obcego mapowania za przywracanym przyciskiem. `_restore_if_owned_locked` nadal łączy surowe bloki bez separatora. Powstaje `KeyF8- name: External LeftTop`, odrzucony przez parser; Manage Modules pozostaje `pending=True`, a mapowania Companion są nadal aktywne.

**P3, ograniczony przypadek odporności ustawień.** Standardowy capture GetProfileYaml serializuje dane z końcowym LF, więc nie traktuję tego jako typowego scenariusza urządzenia ani równoważnego błędu P2. Separator z poprawki B6 należy zastosować również w restore. [third_lifecycle_remap_unload_separator.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_lifecycle_remap_unload_separator.py>) używa pełnego parsowania YAML i atrap InputPlumber.

### Powtórzona weryfikacja

- Backend: **479 testów, 461 zaliczonych i 18 pominiętych na Windows**, bez błędów.
- Frontend: **wszystkie 11 zestawów zaliczone**. Osobna próba [cross-mount kolejki wibracji](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/third_frontend_cross_mount.cjs>) potwierdza prawidłową kolejność odłożonych gestów oraz nowego Reset.
- TypeScript, także strict-unused/unreachable: zaliczony. `npm audit`: **0 podatności**.
- Ponowny build i walidacja wygenerowanego bundle: zaliczone. Payload check: **40 pozycji**.
- Dokładny walidator ZIP z CI: CRC, wymagane pliki, ścieżki, licencje oraz zgodność z aktualnymi źródłami zaliczone. Istniejący ZIP nie wymagał ponownego pakowania; nadal **480 650 bajtów**, SHA-256 `c9b465724b3378d9a0b34f18b5368ff9c388f16342b72efd47317ece0dbe3303`.
- `git diff --check`: zaliczony. HEAD niezmieniony; nie uruchamiano zdalnego CI.

Przejście standardowych testów i poprawny ZIP nie zamykają C1–C7. Nowe próby pokazują luki zestawu regresji i pozostają poza nim do kolejnej iteracji napraw. Odrzucono podejrzenie błędu Disabled po weryfikacji rzeczywistego parsera. Arbitralny wybór pierwszego AppId przy kilku żywych grach pozostaje ograniczeniem fallback `/proc`, który sam nie ustala foreground; nie dodano go do listy potwierdzonych usterek. Testów zapisu/readback na fizycznym Legion Go 2 ani Steam CEF nie wykonywano.

## Status drugiej iteracji poprawek — B1–B9

Poniższa sekcja dokumentuje drugą iterację i jej ówczesne wyniki. Aktualny status napraw C1–C7 i D1–D2 znajduje się na początku dokumentu.

Wszystkie **9 problemów B1–B9 naprawiono w lokalnym kodzie** na bazie `61bad19bde08d937c4678ddc73c3367401b0a0ec`. Zachowano wcześniejsze poprawki A1–A8. Zmiany pozostają niecommitowane, bez pusha i bez operacji na konsoli. Dodano **17 testów backendu** oraz dwa zestawy frontendu z **34 scenariuszami regresji**.

| ID | Naprawa | Regresja |
| --- | --- | --- |
| B1 | Baseline jasności i HDR jest zapisywany atomowo przed pierwszą zmianą atomu gamescope. Błąd zamyka gate, pozostawia baseline pusty i blokuje także writer ze starym `active=True`. | Błąd trwałego zapisu, brak zapisu do sprzętu, udany retry, reload tej samej sesji i przywrócenie 250 nitów; osobny scenariusz HDR |
| B2 | Handoff do `balanced` używa dostępnego node profilu niezależnie od odczytu pełnego zestawu PPT; nadal wymaga readback. | Rzeczywiste pliki tymczasowe z brakującym `max_value`, uninstall i disable, odmowa sukcesu przy zignorowanym zapisie |
| B3 | Setter oraz reset wibracji rozwiązują app/profile raz pod blokadą, używając tego samego kontekstu do guardu, zapisu i odpowiedzi. | Fallback procesów 111→222 podczas operacji, zmiana gry lub overwrite przed resetem, zgodność resetu bez argumentów i forwarding guardów RPC |
| B4 | Mutacje TDP i wibracji sprawdzają generację operacji oraz kontekst gry przed przyjęciem sukcesu, błędu lub kolejnego odczytu. | Spóźnione toggle/delete/preset/advanced/AC, A→B→A, unmount, opóźniony raport gry oraz stary snapshot inicjalizacji/power source |
| B5 | Reset flushuje odłożone gesty i wykonuje się po nich w tej samej kolejce zapisów wibracji. Callbacki debounce są idempotentne. | Reset w oknie debounce oraz podczas zapisu, wielokrotne kliknięcie, anulowany callback czekający na wykonanie, zachowanie draftu i zapis po zamknięciu |
| B6 | Każdy zachowany blok remapera ma separator przed następnym blokiem. | Baseline Desktop F7 bez końcowego LF i dopisany Page F2; semantyczna weryfikacja oraz dodatkowe pełne parsowanie przez PyYAML 6.0.3 |
| B7 | OLED propaguje wyjątek nieudanego startu do loadera; udane ponowienie usuwa poprzedni błąd ustawień. | Manage Modules pokazuje brak gotowości i błąd, RPC jest blokowane, workery nie startują; Retry Start odzyskuje gotowość |
| B8 | Pierwszy błąd TDP/Vibration ma komunikat inline i Retry. Wibracje nie pokazują edytowalnych defaults bez odczytu, a automatyczne próby TDP są ograniczone do trzech i zatrzymywane podczas ukrycia. | Pierwsze odrzucone RPC, powolne odczyty równoległe, wielokrotny Retry, ograniczenie liczby prób, ukrycie/unmount oraz skuteczne odzyskanie |
| B9 | Backend zgłasza faktyczną dostępność TDP, przyczynę i możliwość retry helpera. UI blokuje niedostępne sterowanie TDP, zachowuje osobny zakres CPU oraz odświeża capability podczas widocznego oczekiwania na helper. | Brak obu ścieżek i offline, firmware-only, WMI bez custom node, udany retry, świeże Extras=False, CPU global/game, pojedynczy polling i pominięcie odpowiedzi po ukryciu |

Nowe testy: [backend OLED/wibracje](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_second_hardware_audit.py>), [backend TDP i forwarding](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_second_tdp_audit.py>), [remaper](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/test_remap_backend.py>), [frontend wibracji](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_vibration_recovery.cjs>) oraz [frontend TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tests/frontend_tdp_recovery.cjs>). Oba nowe zestawy frontendu są częścią `npm run test:frontend` i istniejącego CI.

Końcowa weryfikacja na Windows / Python 3.10.9 / Node.js 24.18.0:

- Backend: **479 testów, 461 zaliczonych, 18 pominiętych na Windows**, bez błędów.
- Frontend: **wszystkie 11 zestawów zaliczone**, w tym 16 nowych scenariuszy wibracji i 18 TDP. Istniejący test odświeżenia po ukrytej inicjalizacji TDP przechodzi bez osłabienia asercji.
- TypeScript: typecheck oraz dodatkowe `--noUnusedLocals --noUnusedParameters --allowUnreachableCode false` zaliczone.
- Zależności: czyste `npm ci --ignore-scripts`; `npm audit` zgłasza **0 podatności**. Pozostaje ostrzeżenie deprecacji transytywnego `glob@10.5.0`.
- Build: zaliczony z kontrolą JavaScript, globali Steam/Decky, eksportu plugina i source map.
- Pakowanie: **40 pozycji wejściowych**, **85 plików i 14 katalogów**, **480 650 bajtów**. Dokładny walidator CI potwierdza CRC, wymagane pliki, bezpieczne ścieżki, licencje i zgodność bajtów z bieżącymi źródłami.
- Końcowy ZIP rozpakowano do nowego katalogu; czysta instalacja, typecheck, ponowny build oraz payload check przeszły.
- `git diff --check` zaliczony. Nie uruchamiano zdalnego CI.

[ZIP poprawek](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/LegionGo2Companion-1.0.4.zip>) zachowuje wersję manifestu **1.0.4** i nie jest opublikowanym wydaniem. SHA-256: `c9b465724b3378d9a0b34f18b5368ff9c388f16342b72efd47317ece0dbe3303`.

Próby korzystają z atrap urządzeń i plików tymczasowych. Rzeczywiste zapisy/readback oraz działanie w Steam CEF na Legion Go 2 pozostają do sprawdzenia na sprzęcie; lokalny Windows nie zastępuje testów POSIX w Linux CI.

## Wyniki dodatkowego audytu — przed drugą iteracją napraw

Dodatkowy przegląd lokalnego kodu po pierwszej iteracji poprawek potwierdził **9 usterek: 8 o priorytecie P2 i jedną P3**. W tamtym stanie poprawka A3 pozostawała niepełna dla OLED Display. Nowe przypadki ujawniły także regresje zmian A1, A2 i A6. Wcześniejsze stwierdzenie, że wszystkie osiem problemów zostało zamkniętych, było zbyt mocne. Poniższe opisy i wskazania linii zachowują dowody sprzed drugiej iteracji napraw opisanej powyżej.

Zakres obejmował niecommitowane zmiany na bazie `61bad19bde08d937c4678ddc73c3367401b0a0ec`, a nie nowe zdalne `main`. Wszystkie poniższe scenariusze odtworzono lokalnie z atrapami urządzeń lub plikami tymczasowymi. Na etapie samego dodatkowego audytu nie zmieniano ponownie kodu działania plugina, nie przebudowywano wydania ani nie wykonywano pusha.

### B1 Utrata oryginalnej jasności po błędzie zapisu

**P2.** W ścieżce legacy OLED [zapis ustawień](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1523>) połyka błąd `commit`. [Zapamiętanie baseline](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:1947>) ustawia wartość w RAM przed trwałym zapisem, a gate nadal pozwala zmieniać jasność.

Próba: oryginalne 250 nitów, `disk full` przy zapisie baseline, następnie zapis 120 nitów. Po symulacji przeładowania w tej samej sesji gamescope baseline na dysku wynosi `None`; plugin zapamiętuje własne 120 jako wartość oryginalną i przy release przywraca 120 zamiast 250. Zapamiętanie HDR korzysta z tego samego helpera, lecz ta reprodukcja dotyczyła jasności. Przywracanie wymaga trwałego zapisu baseline przed pierwszą zmianą; jego błąd powinien zamknąć gate.

### B2 Odinstalowanie TDP pomija dostępny profil balanced

**P2, regresja A2.** [Wspólna procedura przywracania](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_backend.py:1558>) wymaga pełnych capability WMI nawet przed zwykłym przełączeniem `platform_profile` na `balanced`. Nowy uninstall odziedziczył to ograniczenie; wcześniejszy hook korzystał z dostępnego node profilu bez wymogu odczytu wszystkich limitów PPT.

Próba z rzeczywistymi plikami tymczasowymi: `platform_profile` i `choices` są dostępne, lecz brak jednego `max_value` powoduje `_wmi_caps() == {}`. Bez RyzenAdj uninstall zgłasza `verified ryzenadj is unavailable` i pozostawia `custom`. Zapis `balanced` do tego samego node działa i jest potwierdzony odczytem. Przywracanie profilu należy oddzielić od capability potrzebnych do ustawiania limitów PPT.

### B3 Edycja wibracji zmienia profil innej gry

**P2, regresja A6.** [Setter](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:876>) sprawdza oczekiwany app/profile, a [helper zapisu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:549>) rozwiązuje bieżącą grę ponownie. Po wygaśnięciu raportu frontendu stan procesów może zmienić się pomiędzy tymi odczytami.

Próba: fallback `/proc` kolejno zwraca grę 111 i 222. `set_intensity(3, '111', '111')` zwraca sukces, ale profil 111 pozostaje na poziomie 0, a profil 222 zmienia się z 1 na 3. Sprawdzenie i zapis muszą używać tego samego, raz rozwiązanego kontekstu profilu.

### B4 Spóźniona odpowiedź nadpisuje edytor nowej gry

**P2.** Zmiana profilu w [TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1432>) i [Vibration](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:630>) przyjmuje odpowiedź bez sprawdzenia, czy nadal dotyczy aktualnej gry. Dotyczy to również opóźnienia transportu już po zakończeniu mutacji backendu i zwolnieniu jego blokady.

Próba TDP: wyłączenie profilu 111, przejście do 222 i poprawny odczyt Silent 8 W, następnie spóźniona odpowiedź pierwszej operacji. Edytor gry 222 pokazuje Balanced 16 W, nadal z włączonym Per Game Profile. Następne Custom → Apply wysyła `16000/25000/30000` dla gry 222 i nadpisuje jej zapisany profil. Analogiczna próba wibracji podmienia intensity gry 222 odpowiedzią dla 111. Każda mutacja i jej obsługa błędów potrzebują sprawdzenia generacji oraz kontekstu gry przed zmianą UI.

### B5 Opóźniony suwak cofa reset wibracji

**P2.** [Reset](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:595>) omija kolejkę zapisów oraz oczekujące zmiany z debounce. `applying` ustawia się dopiero po uruchomieniu odłożonego zapisu, więc Reset jest aktywny w 150 ms oknie po zmianie suwaka.

Próba: suwak intensity → 3, natychmiast Reset → domyślne 2, następnie wykonanie timera suwaka → zapisane i widoczne ponownie 3. Reset powinien anulować albo zakończyć wcześniejsze odłożone zmiany i wykonać się po nich w tej samej kolejce mutacji.

### B6 Default remapera może wygenerować niepoprawny YAML

**P2, regresja A1.** [Łączenie bloków](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:423>) zakłada końcowy LF w każdym zachowanym bloku baseline.

Próba: poprawny baseline zawiera tylko Desktop → F7 i nie ma końcowego LF; Page zostaje ustawiony na F2. Wynik zawiera `keyboard: KeyF7- name: Companion Page`. PyYAML 6.0.3 parsuje baseline, lecz dla wyniku zgłasza `ScannerError`. Własny płytki `_valid_profile` zwraca nadal `True`, a sprawdzenie żądanych mapowań `False`. Należy zagwarantować separator pomiędzy blokami i dodać regresję obejmującą brak końcowej nowej linii.

### B7 Nieudany start OLED nadal wygląda na udany

**P2, pozostałość A3.** [OLED `_main`](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/display_backend.py:2811>) zapisuje błąd ustawień i zwraca `None`, który główny loader interpretuje jako udany start.

Próba: `_read_settings` zgłasza `disk unavailable`. Status Manage Modules wynosi `enabled=True, ready=True, error=''`, oba workery pozostają `None`. Ponowne włączenie OLED nie uruchamia `_main` po raz drugi. Ten moduł także musi zgłosić wyjątek albo jawne `success=False`, aby zadziałały nowa gotowość i Retry Start.

### B8 Pierwszy błąd odczytu TDP i Vibration pozostaje źle obsłużony

**P2, kolejne ekrany z klasy A4.** [TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1241>) po błędzie inicjalizacji wyświetla `Initializing...` i bez komunikatu ponawia pełny odczyt co sekundę. [Vibration](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:499>) po błędzie usuwa loading i renderuje edytowalne `DEFAULT_SETTINGS`, mimo że ustawień nie odczytano.

Próby: odrzucone `get_caps` w TDP daje cztery pełne batche odczytów po trzech timerach, bez błędu ani Retry. Odrzucone `vibe_get_settings` daje intensity 2, toast i aktywne kontrolki, bez inline error/retry inicjalizacji. Watcher może później dostarczyć poprawny stan wibracji; nie oznacza to trwałego braku odzyskania w każdej sesji. Obie strony powinny pokazać błąd odczytu i bezpieczne ponowienie, a ustawienia powinny stać się edytowalne dopiero po potwierdzonym odczycie.

### B9 Kontrolki TDP są aktywne przy braku obu ścieżek sterowania

**P3.** Gdy WMI jest niedostępne, lokalnego RyzenAdj brak, a pobranie kończy się błędem, [start TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_backend.py:2932>) nadal daje `ready=True, error=''`. `get_caps` zwraca zakresy fallback z `wmi=False, extras=False`, lecz [inicjalizacja UI](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1217>) nie używa `wmi` do określenia dostępności TDP. Na świeżym profilu ostrzeżenie i Retry helpera nie pojawią się, ponieważ [sekcja wymaga](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/tdp.tsx:1857>) `extrasUnlocked=True`.

Próba backendu po nieudanym pobraniu: root oraz `is_ready` zgłaszają gotowość, a rzeczywisty dispatch `_apply_limits` zwraca `verified ryzenadj is unavailable`. Osobna próba rzeczywistego TSX z `wmi=False, extras=False, extrasUnlocked=False` potwierdza aktywne Apply TDP i brak Retry helpera. To problem prezentacji dostępności, nie udany zapis sprzętowy. Osobny stan dostępności TDP powinien pokazać przyczynę i Retry; sprawne CPU Boost/EPP mogą pozostać dostępne niezależnie.

### Weryfikacja i reprodukcje

Standardowy zestaw nadal przechodzi: **462 testy backendu, 444 zaliczone i 18 pominiętych na Windows**, oraz wszystkie **9 zestawów frontendu**. Nowe próby pokazują scenariusze nieobjęte tym zestawem. Build i ZIP z pierwszej iteracji pozostają bez zmian; ich poprawne zbudowanie nie potwierdza opisanych zachowań runtime.

Reprodukcje znajdują się poza checkoutem:

- [OLED startup, baseline i backendowa zmiana profilu wibracji](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_hardware_repro.py>) — `python further_hardware_repro.py`.
- [YAML remapera bez końcowego LF](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_lifecycle_remap_newline.py>) — `python further_lifecycle_remap_newline.py`; pełne parsowanie przez istniejący `audit-tools-venv/Scripts/python.exe`.
- [Cleanup TDP bez pełnych capability](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_lifecycle_tdp_release_profile.py>) — `python further_lifecycle_tdp_release_profile.py`.
- [TDP bez obu ścieżek sterowania](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_lifecycle_tdp_no_path.py>) — `python further_lifecycle_tdp_no_path.py`.
- [Frontend TDP bez obu ścieżek sterowania](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_frontend_no_path.cjs>) — `node further_frontend_no_path.cjs`.
- [Reset i spóźniona odpowiedź Vibration](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_frontend_vibration.cjs>) — `node further_frontend_vibration.cjs`.
- [Spóźniona odpowiedź i kolejny zapis TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_frontend_tdp.cjs>) — `node further_frontend_tdp.cjs`.
- [Pierwsze błędy odczytu TDP i Vibration](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/further_frontend_initial_errors.cjs>) — `node further_frontend_initial_errors.cjs`.

Przegląd nie zastępuje testów zapisu/readback na fizycznym Legion Go 2 ani działania w Steam CEF. Nie wykonywano zapisów na konsoli.

## Status poprawek — 9 października 2026

Pierwsza iteracja lokalnych zmian objęła **A1–A8** na bazie audytowanego commita. Zmiany są niecommitowane i nie zostały wypchnięte. Poniższe zestawienie opisuje tę iterację; późniejszy dodatkowy audyt powyżej wskazuje pozostałe błędy i regresje. Dalsza część raportu zachowuje historyczne wyniki oraz dowody sprzed napraw.

| ID | Zmiana | Regresja |
| --- | --- | --- |
| A1 | Default używa przechwyconego bloku przycisku; brak mapowania pozostaje brakiem. Generowanie, weryfikacja i ownership używają tego samego oczekiwanego mapowania. | F7/F8, pojedynczy Default, zewnętrzna zmiana innego przycisku, brak mapowania, nierozpoznany blok i wykrycie jego zmiany |
| A2 | Disable i uninstall współdzielą przywracanie TDP, CPU Boost oraz EPP, także z profili gry/AC. Każda zarządzana kontrolka jest próbowana mimo awarii innej; wyniki wymagają potwierdzenia. | Wyłączone TDP, kontrolki zarządzane tylko w grze/AC, częściowa awaria, brak zarządzanych kontrolek, zachowanie preferencji |
| A3 | Gotowość jest oddzielona od zapisanego wyboru włączenia. Błąd trafia do Manage Modules, blokuje RPC i pozwala ponowić start po zatrzymaniu i drain częściowej pracy. WiFi przekazuje jawny wynik startu. | Błędy RGB/remap/battery, jawny błąd WiFi, awaria cleanup, skuteczne ponowienie i wyczyszczenie błędu wibracji |
| A4 | Cztery ekrany pokazują błąd pierwszego odczytu oraz Retry status, zachowując ograniczenie współbieżnych odczytów i ochronę przed starymi odpowiedziami. | Odrzucone pierwsze i kolejne RPC, wielokrotne kliknięcie Retry, ukrycie strony i skuteczne ponowne otwarcie |
| A5 | Helper jest sprawdzany lokalnie, a pobieranie działa w tle poza wspólną blokadą RPC. Transfer ma limit 120 s sprawdzany przed i po odczycie, timeout operacji sieciowej do 3 s, anulowanie oraz sprzątanie częściowych plików. | Powolny strumień, anulowanie podczas odczytu, timeout bez nadpisania helpera, start firmware podczas pobierania, unload bez późnej aktywacji, brak równoległych duplikatów i skuteczny retry |
| A6 | TDP i wibracje współdzielą wygasający raport gry oraz fallback do procesów Steam. Wibracje wracają do profilu globalnego lub aktualnie wykrytej gry po utracie frontendu. | Wygaśnięcie po 12 s, cofnięcie zegara, świeży raport zakończenia gry, nadal uruchomiona/inna gra, heartbeat i powrót frontendu |
| A7 | Świeży zapis wibracji wymaga zgodnego odczytu przed sukcesem i cache; maksymalnie trzy próby z krótką przerwą. | Potwierdzony/opóźniony odczyt, zignorowany zapis, rozłączenie urządzenia oraz częściowa awaria całego profilu |
| A8 | Usunięto `@decky/rollup` i podatny łańcuch jego pluginu czyszczenia. Lokalna konfiguracja zachowuje globale Decky i licencję; `brace-expansion` ma wersję 2.1.7. Build sprawdza wynikowy bundle, a CI kontroluje advisories. | Czysta instalacja z lockfile, `npm audit`, build z kontrolą globali/eksportu/source map, integralność ZIP i ponowny build z ZIP |

Końcowe sprawdzenia wykonano lokalnie na Windows / Python 3.10.9 / Node.js 24.18.0:

- Backend: **462 testy, 444 zaliczone i 18 pominiętych**, bez błędów. Przybyło 31 testów względem audytu.
- Frontend: **9 zestawów zaliczonych**, w tym nowe scenariusze pierwszego błędu statusu i Retry Start.
- TypeScript: zwykły typecheck oraz `--noUnusedLocals --noUnusedParameters --allowUnreachableCode false` zaliczone.
- Build: zaliczony wraz z analizą wygenerowanego JavaScript, globali Steam/Decky, eksportu plugina i source map.
- Zależności: `npm ci --ignore-scripts` zaliczone; `npm audit` zgłasza **0 podatności**. Pozostaje komunikat deprecacji transytywnego `glob@10.5.0`; nie jest on wynikiem advisory w tym sprawdzeniu.
- Pakowanie: payload check **40 pozycji**; ZIP **85 plików i 14 katalogów**, **471 852 bajty**. Dokładny walidator z CI potwierdza CRC, wymagane pliki, bezpieczne ścieżki, licencje i zgodność bajtów ze źródłami. Lista wejściowa zawiera wyłącznie dwa potrzebne skrypty builda/pakowania i nie włącza prywatnych helperów urządzenia.
- Rozpakowane archiwum: ponowna czysta instalacja zależności, typecheck i build zaliczone; źródła oraz nowy walidator builda są kompletne.
- `git diff --check`: zaliczony. Nowa konfiguracja CI została sprawdzona lokalnie; nie uruchamiano jej zdalnie.

ZIP `LegionGo2Companion-1.0.4.zip` jest lokalnym artefaktem poprawek, bez zmiany wersji manifestu i bez publikacji wydania. SHA-256: `dd3dc984ae309fff30b23c499f7c16461622f5e173dcca14dc3e1894fbc66d15`.

Nie instalowano plugina ani nie zapisywano ustawień konsoli. Testy urządzeń korzystają z atrap i plików tymczasowych. Walidacja rzeczywistych zapisów/readback, cleanup i zachowania w Steam CEF na Legion Go 2 pozostaje do wykonania na sprzęcie; lokalny Windows nie zastępuje macierzy Linux CI.

## Raport pierwotnego audytu

Data audytu: **9 października 2026**. Repozytorium: [Rayekkk/LegionGo2Companion](https://github.com/Rayekkk/LegionGo2Companion). Zakres: gałąź `main`, commit `61bad19bde08d937c4678ddc73c3367401b0a0ec`, pobrany i ponownie porównany z zdalnym `main`.

Plugin ma solidne zabezpieczenia ustawień, rozbudowane procedury odzyskiwania i działający proces budowania. **Wymaga jednak poprawek przed kolejnym wydaniem:** pięć usterek dotyczy przywracania ustawień, stanu modułów, obsługi błędów i opóźnień uruchamiania; dodatkowa usterka dotyczy utrzymania nieaktualnego profilu wibracji. Dwa problemy o niższym priorytecie dotyczą potwierdzania zapisów wibracji i zależności narzędzi budowania.

Nie potwierdzono problemu P0 ani P1 w sprawdzonym kodzie i próbach. Ta ocena dotyczy źródeł i izolowanych testów. Skuteczność zapisu, przywracania i współpracy ze SteamOS pozostaje zależna od testów na rzeczywistym urządzeniu.

## Zakres i wersja

Przegląd objął wszystkie 19 własnych modułów Python, strony TypeScript/React, dwa skrypty Lua, migracje, publiczne RPC, start i zatrzymanie modułów, odinstalowanie, pliki odzyskiwania, pobieranie aktualizacji, zależności, licencje, CI i zawartość archiwum. Bibliotekę pyudev porównano z opublikowanym pakietem 0.24.4.

Manifest, `package.json` i lockfile nadal podają **1.0.4**. Publiczne [wydanie 1.0.4](https://github.com/Rayekkk/LegionGo2Companion/releases/tag/1.0.4) opublikowano 23 września 2026 o 21:47 UTC. Audytowane `main` zawiera późniejsze zmiany opisane w sekcji **Unreleased**, m.in. przekazanie obsługi OLED do natywnego gamescope i zmiany TDP. Wnioski dotyczą tego commita, a nie automatycznie starszego ZIP z wydania.

Kopia lokalna znajduje się w `C:\Users\Rayek\Desktop\Codex\Decky Plugins\LegionGo2Companion`. Audyt nie zmienił kodu działania plugina, ustawień konsoli ani repozytorium zdalnego. Nowym dokumentem w kopii projektu jest ten raport. Skrypty prób i surowe wyniki zapisano poza checkoutem.

## Wyniki sprawdzeń

Środowisko lokalne: Windows, Python **3.10.9**, Node.js **24.18.0**, npm **11.16.0**. Narzędzia dodatkowe: coverage **7.6.12** i Bandit **1.8.6**, w osobnym środowisku wirtualnym poza projektem.

| Sprawdzenie | Wynik |
| --- | --- |
| Instalacja zależności z lockfile przez `npm ci --ignore-scripts` | Poprawna |
| Backend `python -m unittest discover -s tests -p 'test_*.py'` | 431 testów, **413 zaliczonych i 18 pominiętych**, bez błędów |
| Frontend `npm run test:frontend` | Wszystkie **9 zestawów** zaliczone |
| `npm run typecheck` | Poprawny |
| Dodatkowy TypeScript z `--noUnusedLocals --noUnusedParameters --allowUnreachableCode false` | Poprawny |
| `npm run build` | Poprawny |
| `npm run package -- --check` | Poprawny, 36 pozycji wejściowych |
| `npm run package` | Poprawny ZIP, 81 plików i 13 katalogów, 468 621 bajtów |
| Dokładny skrypt kontroli archiwum z CI | Poprawny CRC, ścieżki, wymagane pliki, brak duplikatów i symlinków, zgodność bajtów ze źródłami |
| `git diff --check` przed dodaniem raportu | Poprawny |
| `npm audit` | 8 wpisów o najwyższej wadze **high**, wszystkie w drzewie zależności deweloperskich |
| `npm audit --omit=dev` | 0 zgłoszeń; nie obejmuje bibliotek pakowanych z devDependencies, kodu Python ani usług systemowych |
| Bandit na własnych modułach Python | 33 sygnały: 25 low, 8 medium, 0 high; ocena poniżej |
| Porównanie pyudev z wheel 0.24.4 | Wszystkie **24 pliki Python identyczne bajtowo**, brak dodatkowych lub brakujących plików |

[CI dla dokładnie tego commita](https://github.com/Rayekkk/LegionGo2Companion/actions/runs/36460213215) zakończyło się sukcesem w trzech zadaniach: backend Ubuntu 24.04/Python 3.10, backend Windows 2022/Python 3.13 oraz frontend i pakowanie/Node.js 24. Linux CI uzupełnia lokalne testy Windows, lecz również korzysta z atrap sprzętu.

18 lokalnych pominięć obejmuje m.in. semantykę POSIX, uprawnienia sysfs, `dir_fd`, trwałość katalogów i symlinki. Testy frontendowe wykonują rzeczywiste komponenty w kontrolowanym środowisku VM z atrapami React/Decky; nie są testem rzeczywistego Steam CEF ani nawigacji na fizycznym kontrolerze.

Zbudowany ZIP jest artefaktem audytu z bieżącego `main`. Jego nazwa `LegionGo2Companion-1.0.4.zip` wynika z manifestu i nie oznacza zgodności ze starszym publicznym wydaniem. SHA-256: `4ed485df11aeccc6403cbfac093c5363b5377dcbde5907bf7a3606c944c5f868`.

## Potwierdzone problemy

P2 oznacza usterkę do naprawienia w najbliższej iteracji. P3 oznacza ulepszenie o mniejszym wpływie lub ryzyko ograniczone warunkami. Priorytet projektu jest osobną oceną od poziomu advisory lub skanera.

| ID | Priorytet | Problem | Dowód |
| --- | --- | --- | --- |
| A1 | P2 | Default remapera nie przywraca przechwyconej akcji przycisku | Rzeczywista funkcja generowania profilu, własne F7/F8 |
| A2 | P2 | Odinstalowanie pomija przywrócenie zarządzanego CPU Boost i EPP | Wywołanie rzeczywistego hooka z atrapami sysfs |
| A3 | P2 | Nieudany start modułu pozostaje włączony bez błędu w Manage Modules | Wstrzyknięty wyjątek podczas startu RGB |
| A4 | P2 | Pierwszy błąd odczytu statusu daje sam spinner na czterech ekranach | Rzeczywiste komponenty, odrzucone pierwsze RPC |
| A5 | P2 | Pobieranie RyzenAdj nie ma limitu całkowitego czasu i może wstrzymać RPC | Analiza blokady i downloadera oraz symulacja powolnej odpowiedzi |
| A6 | P2 | Profil wibracji może pozostać przypisany do zakończonej gry po utracie frontendu | Wybór profilu po symulowanym upływie doby |
| A7 | P3 | Świeży zapis wibracji zwraca sukces bez natychmiastowego odczytu | Zapis z atrapą pliku, zero wywołań odczytu |
| A8 | P3 | Lockfile zawiera podatne narzędzia przetwarzania wzorców w buildzie | `npm audit`, `npm ls` i advisories |

### A1 Default nadpisuje własne mapowanie użytkownika

[remap_backend.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:288>) zamienia `default` na stałe akcje: Desktop → `Guide + North`, Page → `Screenshot`. [Generowanie profilu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/remap_backend.py:363>) używa ich również wtedy, gdy `baseline_profile` zawiera inne mapowania. Przycisk przywracania domyślnych akcji korzysta z tej samej ścieżki. Jest to sprzeczne z [opisem Default w README](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/README.md:252>), który obiecuje przywrócenie przechwyconego zachowania systemowego.

**Odtworzenie:** bazowy profil zawiera Desktop → F7 i Page → F8. Przy włączonym remapowaniu ustawienie obu akcji na Default usuwa F7/F8 i wstawia wymienione stałe akcje. Wyłączenie całego remapera ma odrębną procedurę przywracania; problem dotyczy Default podczas aktywnego remapowania.

**Poprawka:** dla Default odtworzyć blok właściwego przycisku z zapisanego baseline, zachowując pozostałe aktualne mapowania. Ustalić zachowanie, gdy w baseline nie ma danego przycisku. Weryfikator profilu musi sprawdzać to samo oczekiwane zachowanie.

**Test regresji:** niestandardowe F7/F8, tylko jeden przycisk na Default, mieszana zmiana przez zewnętrzny profil i ponowne wykrycie InputPlumber.

### A2 Odinstalowanie pozostawia osobne ustawienia CPU bez przywrócenia

[Hook odinstalowania TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_backend.py:2946>) ustawia wyłącznie firmware `platform_profile` na `balanced`. Nie wykonuje przywracania CPU Boost ani EPP. Tymczasem [wyłączenie modułu TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/module_control.py:76>) jawnie przywraca zarządzane ustawienia do boost włączonego i `balance_performance`. Te osobne kontrolki są ustawiane przez własne operacje sysfs.

**Odtworzenie:** zapisane `cpu_boost_enabled=False` oraz `epp=power`. Hook uninstall wykonał ustawienie `balanced`, lecz liczba wywołań przywrócenia boost/EPP wyniosła 0/0. Wyłączenie modułu wykonało oba przywrócenia. To potwierdza brak operacji w kodzie; rzeczywiste wartości pozostające po zmianie profilu firmware trzeba odczytać na urządzeniu.

**Poprawka:** użyć wspólnej procedury oddawania kontroli dla wyłączenia modułu i odinstalowania. Przywracać tylko kontrolki, które plugin zarządzał, sprawdzać odczyt zwrotny i raportować niepowodzenie. Uwzględnić ograniczony czas zamykania procesu Decky.

**Test regresji:** boost wyłączony, EPP zmienione globalnie lub przez profil gry, TDP wyłączone przy aktywnych kontrolkach CPU oraz niepowodzenie jednego przywrócenia.

### A3 Nieudany start nie trafia do statusu modułu

[main.py](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:305>) uruchamia moduły przez `gather(return_exceptions=True)`, a błędy jedynie zapisuje w logu. Nie aktualizuje ich `error`, gotowości ani możliwości ponowienia. [Start całego plugina](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:441>) oznacza moduły jako uruchomione przed zakończeniem tej fazy. [Ponowne włączenie](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:255>) od razu zwraca status, jeśli moduł już ma `enabled=True`.

**Odtworzenie:** `RGB._main()` zgłasza wyjątek. Status nadal wynosi `enabled=True, pending=False, error=""`. Ponowne włączenie RGB nie wywołuje startu po raz drugi. Rzeczywisty start RGB może zgłosić wyjątek np. przy nieodzyskiwalnym błędzie ustawień; nie jest to wyłącznie sztuczna możliwość interfejsu.

**Poprawka:** rozdzielić zapisany wybór użytkownika od gotowości modułu, przekazywać błąd startu do Manage Modules i zapewnić bezpieczne ponowienie. Powtórzenie startu musi najpierw uporządkować ewentualne częściowo utworzone zadania. Moduły zwracające błąd bez wyjątku powinny mieć wspólny kontrakt wyniku startu.

**Test regresji:** wyjątek RGB/remap/battery podczas startu, błąd zapisu ustawień, częściowy start oraz skuteczne ponowienie bez restartu całego plugina.

### A4 Pierwszy błąd statusu jest ukryty przez spinner

[Remapper](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/remap.tsx:153>), [RGB](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/rgb.tsx:266>) i [WiFi](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/wifi.tsx:156>) zapisują komunikat błędu odczytu, ale wcześniejsza gałąź renderowania zwraca sam spinner, dopóki nie ma pierwszego poprawnego statusu. [OLED Display](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/display.tsx:150>) ignoruje błąd odczytu i [również pokazuje spinner](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/display.tsx:261>).

**Odtworzenie:** odrzucono pierwsze RPC statusu każdego z czterech rzeczywistych komponentów. Wszystkie wyświetliły spinner, żaden komunikat błędu. Jeśli backend stale nie może przeczytać ustawień, ekran może pozostać w tym stanie mimo kolejnych prób. Błąd po wcześniejszym poprawnym statusie ma inny przebieg.

**Poprawka:** pokazywać błąd pierwszego odczytu, krótki opis ponawiania i przycisk ponownej próby. Zachować obecne ograniczanie liczby odczytów oraz ochronę przed spóźnionymi odpowiedziami.

**Test regresji:** pierwsza odpowiedź odrzucona, kilka kolejnych błędów, potem sukces; ukrycie i ponowne otwarcie strony w trakcie tych prób.

### A5 Powolne pobieranie pomocnika może zablokować uruchomienie

[tdp_updater.download_to](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_updater.py:120>) ogranicza rozmiar do 32 MiB, ale nie mierzy całkowitego czasu. Przekazane 30 sekund ogranicza blokujące operacje sieciowe; nie stanowi terminu zakończenia całego transferu. [Dokumentacja urllib](https://docs.python.org/3/library/urllib.request.html#urllib.request.urlopen) opisuje właśnie timeout takich operacji.

Na Go 2, przy brakującym lub niepoprawnym pomocniku, [start TDP](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/tdp_backend.py:2862>) czeka na pobranie przed uruchomieniem workerów, również gdy użytkownik korzysta tylko ze standardowego zakresu firmware. [Start Companion](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/main.py:433>) czeka na zakończenie modułów pod wspólną blokadą dopuszczania RPC. Retry Extras również przechodzi przez tę blokadę.

**Odtworzenie:** odpowiedź testowa dostarcza 10 porcji, każda odpowiada 29 sekundom czasu transferu. Downloader kończy po symulowanych 290 sekundach mimo `timeout=30`. Próba nie wykonywała rzeczywistego powolnego transferu; potwierdza brak sprawdzania terminu zakończenia w pętli. Sam limit bajtów i poprawna suma SHA-256 pozostają skuteczne.

**Poprawka:** dodać termin oparty na zegarze monotonicznym oraz przerywanie/odrzucanie niepełnego pobrania. Uruchomić firmware TDP bez czekania na opcjonalne Extras, a pobieranie prowadzić poza wspólną blokadą sprzętowych RPC. W projekcie jest już podobny mechanizm limitu czasu w `companion_updates.py`.

**Test regresji:** porcje regularnie dostarczane przed timeoutem socketu, ale po przekroczeniu całkowitego limitu; równoległy odczyt baterii; anulowanie/zamknięcie podczas pobierania.

### A6 Wibracje nie wygaszają kontekstu gry

[Wybór profilu wibracji](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:417>) korzysta z ostatniego `_active_app_id` bez sprawdzania wieku informacji lub działającego procesu. Frontend wysyła heartbeat co 6 sekund, lecz [zatrzymanie watchera](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/src/vibration.tsx:274>) nie zwalnia tego kontekstu po stronie backendu. Hotplug i kontrola driftu nadal stosują tak wybrany profil.

**Warunek:** frontend lub jego watcher przestaje działać, backend pozostaje aktywny, a gra kończy się bez kolejnego poprawnego `set_active_app`. Normalne zamknięcie QAM nie spełnia tego warunku, ponieważ watcher jest globalny. Przy sprawnym watcherze profil zmienia się poprawnie.

**Odtworzenie:** globalny poziom wibracji 2, profil gry 111 z poziomem 0 i włączonym overwrite. Po przesunięciu zegara o dobę, bez nowego raportu z frontendu, wybór nadal daje poziom 0. Nie ma mechanizmu wygaszenia, który pozwoliłby wrócić do profilu globalnego.

**Poprawka:** dodać odnawiany termin ważności kontekstu i bezpieczny wybór profilu po jego wygaśnięciu, najlepiej wspólny z mechanizmem TDP. Uwzględnić możliwość dalszego działania gry mimo chwilowej awarii frontendu.

**Test regresji:** utrata heartbeatu, zakończenie gry, gra nadal działająca, powrót frontendu, hotplug i wznowienie w trakcie utraty kontekstu.

### A7 Sukces zapisu wibracji wyprzedza jego potwierdzenie

[vibration_backend._write_attr](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/LegionGo2Companion/vibration_backend.py:254>) po świeżym zapisie zwraca `True` i aktualizuje cache bez natychmiastowego odczytu sysfs. Odczyt występuje dopiero przy późniejszym trafieniu w cache. Operacja zbiorcza traktuje te wartości jako wynik sukcesu wszystkich zapisów.

**Odtworzenie:** wymuszony zapis z atrapą pliku zwraca sukces, a liczba wywołań odczytu wynosi zero. To nie dowodzi, że rzeczywisty sterownik ignoruje zapis; dowodzi, że bieżąca odpowiedź nie potwierdza wartości po zapisaniu.

**Poprawka:** po nowym zapisie odczytać wartość i porównać ją z żądaną, dopiero potem potwierdzić wynik i cache. Jeśli ABI dopuszcza opóźnienie, zastosować ograniczone ponowienia oraz osobny stan oczekiwania na potwierdzenie.

**Test regresji:** zapis zaakceptowany, odczyt nadal stary; odczyt pojawia się z opóźnieniem; kontroler znika podczas zapisu; jeden z pięciu atrybutów nie potwierdza zmiany.

### A8 Zależności builda zawierają znane podatności

Lockfile instaluje `braces 3.0.3` i `brace-expansion 1.1.18 / 2.1.4`. Osiem zgłoszeń npm obejmuje te biblioteki i ich łańcuch zależności: `@decky/rollup → rollup-plugin-delete → del/globby/fast-glob/micromatch` oraz zależności glob/minimatch.

Advisories dotyczą przeciążenia CPU lub stosu przy specjalnie przygotowanych wzorcach. Dla brace-expansion dostępne są poprawki odpowiednich gałęzi **1.1.21 i 2.1.7**, obejmujące sprawdzony alert złożoności kwadratowej. [Advisory brace-expansion](https://github.com/advisories/GHSA-q2hr-2g5m-vwhr), [advisory rekurencji](https://github.com/advisories/GHSA-qhr7-859c-m2p7).

Dla sprawdzonego alertu `braces <=3.0.3` baza GitHub w dniu audytu nie wskazuje wersji naprawionej. [Advisory braces](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).

**Wpływ:** ryzyko dotyczy narzędzi budowania i zależy od dostarczenia im problematycznego wzorca. W sprawdzonym procesie builda nie potwierdzono ścieżki dostarczania takich wzorców z UI plugina. Biblioteki te nie są wykonywane jako część backendu na konsoli. Osiem wpisów npm nie oznacza ośmiu odrębnych podatności aplikacji na SteamOS. Wynik `--omit=dev` sam w sobie nie dowodzi bezpieczeństwa całego runtime, bo projekt używa devDependencies także do budowania i pakowania frontendu.

**Poprawka:** odświeżyć lockfile dla naprawionych gałęzi brace-expansion. Dla pozostałego łańcucha rozważyć aktualizację presetów Decky lub usunięcie zależności od starego pluginu usuwania plików, z kontrolą zgodności builda. Nie stosować automatycznego `audit fix --force` bez oceny wynikowych zmian.

**Test regresji:** czyste `npm ci`, typecheck, wszystkie zestawy frontendu, build, kontrola licencji i zgodności ZIP. CI powinno raportować nowe advisories także pomiędzy commitami.

## Mocne strony i ocena modułów

| Obszar | Co jest dobrze rozwiązane | Pozostałe istotne uwagi |
| --- | --- | --- |
| Ustawienia i migracje | Atomowy zapis, fsync, backup, ograniczenie rozmiaru/głębokości JSON, rozróżnienie pierwszej instalacji od utraty danych, kontrola symlinków i ścieżek POSIX | Testy Windows nie uruchamiają wszystkich gwarancji POSIX; pomiar pokrycia nie zastępuje prób awarii procesu i dysku |
| Główna kompozycja | Publiczne RPC objęte bramką; konflikty z trzema poprzednimi pluginami, również po zmianie nazwy; blokada przy niepewnej identyfikacji; zachowanie transakcji przy anulowaniu | A3; wspólna blokada serializuje także odczyty innych modułów podczas długiej operacji sprzętowej |
| TDP i CPU | Oddzielone zapisane ustawienia i kontekst gry/AC; obsługa wznowienia; transakcyjne CPU Boost/EPP z rollback i readback; weryfikacja SHA-256 RyzenAdj | A2 i A5; SPL zastępowane ostatnio zastosowanym żądaniem na nieczytelnym firmware nie jest niezależnym pomiarem limitu sprzętowego |
| Wibracje | Obsługa hotplug, wznowienia i driftu; ochrona edycji przed zmianą gry; ograniczanie odczytów frontendu | A6 i A7; słabe lokalne pokrycie backendu |
| OLED i EDID | Jawne przejęcie przez native gamescope; oddzielny EDID; rozpoznawanie własnych skryptów; zachowanie obcych plików; trwały stan odzyskiwania i wymagany restart sesji | A4; native support jest świadomie wykrywane po obecności wskazanego pliku systemowego, a nie po jego zawartości; aktualnie załadowana sesja wymaga osobnego rozpoznania |
| WiFi | Dziennik transakcji i zewnętrzny mechanizm rollback; sprawdzanie faktycznej częstotliwości; cross-process lock; zachowanie cudzych zmian; brak automatycznej zmiany backendu NetworkManager | A4; długie reconnecty wymagają pomiaru responsywności innych RPC; część wywołań startup/power-save jest synchroniczna wewnątrz async |
| RGB | Snapshot stanu, dziennik odzyskiwania, rollback, readback i retry; bezpośredni rejestr LED ograniczony modelem, BIOS i hashem DSDT | A3/A4; Power LED wymaga właściwego BIOS/DSDT, nie rozszerzać wsparcia na podstawie samej nazwy urządzenia |
| Remapper | Modyfikacja dwóch dedykowanych przycisków, zachowanie reszty profilu, wykrywanie ponownego startu InputPlumber, przywracanie tylko nadal posiadanych mapowań | A1/A4; custom baseline powinien być przypadkiem podstawowym w testach |
| Bateria | Tryby oferowane przez ABI, ograniczenie do urządzenia, przechwycenie wcześniejszego stanu, journal przed zapisem, readback/rollback, oddanie kontroli | Lokalny zestaw ma mocniejsze pokrycie, lecz rzeczywiste zachowanie firmware Standard/Fast/Long Life wymaga odczytu na urządzeniu |
| Gyro i diagnostyka | Weryfikacja fizycznego urządzenia/sterownika/interfejsu, transakcja filtrów InputPlumber i sysfs, zachowanie niepowiązanych filtrów; ograniczona czasowo pasywna diagnostyka bez przejęcia wejścia | Testy parsera i fixture nie potwierdzają opóźnienia, poprawności osi ani braku utraty raportów na rzeczywistym urządzeniu |
| Aktualizacje Companion | Wyłącznie ręczne sprawdzanie/pobieranie; HTTPS, kontrola przekierowań, digest, limity czasu/rozmiaru/ZIP, bezpieczny katalog docelowy i atomowa publikacja; brak automatycznej instalacji | Zaufanie do digest opublikowanego w tym samym GitHub release nie jest niezależnym podpisem wydania |
| Frontend i pakowanie | Strict TypeScript, ochrona przed starymi odpowiedziami, polling zależny od widoczności, zachowana nawigacja/focus, źródła i licencje w ZIP | A4; testy VM nie zastępują rzeczywistego CEF; przed publikacją nadać wersję odróżniającą nowe main od publicznego 1.0.4 |

Skrypty Lua dla PQ i gamma22 mają wspólną strukturę i różnią się ustawieniem transferu. Ich dobór parametrów panelu oraz działanie 48–144 Hz wymagają pomiarów; poprawna struktura kodu i testy backendu nie potwierdzają kolorów, luminancji ani stabilności obrazu.

Zachowanie WiFi przy odinstalowaniu celowo usuwa własny plik ustawień po udanym cleanup. README uprzedza o możliwości ponownej konfiguracji. Warto ujednolicić tę politykę z pozostałymi modułami, które zachowują preferencje, ale nie jest to tutaj sklasyfikowane jako nieudokumentowana utrata danych.

## Pokrycie i jakość testów

Pomiar obejmuje własne moduły Python, bez testów, biblioteki standardowej i vendored pyudev. Zliczono **6123/10 209 wykonanych instrukcji, czyli 60,0%**, oraz **1695/3628 wykonanych rozgałęzień, czyli 46,7%**. Łączny wskaźnik coverage z włączonymi branchami wynosi **56,5%**. Są to wyniki zestawu uruchomionego na Windows, nie łączny wynik macierzy CI.

| Moduł lub infrastruktura | Łączne pokrycie instrukcji i rozgałęzień |
| --- | --- |
| Bateria | 85,3% |
| Controller IMU | 84,6% |
| Główny backend | 80,2% |
| Remapper | 76,6% |
| Bezpieczne ustawienia | 71,7% |
| Gyro i diagnostyka | 69,7% |
| TDP | 61,8% |
| Zarządzanie modułami | 61,8% |
| Aktualizacje Companion | 59,1% |
| RGB | 53,8% |
| Display | 53,3% |
| WiFi | 34,8% |
| Wibracje | 34,5% |

Zestaw zawiera wartościowe próby awarii, recovery, ownership i spóźnionych odpowiedzi, ale liczba zaliczonych testów nie pokrywa wszystkich zachowań publicznych. Największą wartość dadzą testy scenariuszy A1–A7 oraz nienormalnego zatrzymania workerów, zamiast dopisywania testów samych getterów. Pomiar branch coverage warto prowadzić na Linux i Windows oraz porównywać zmiany w najbardziej ryzykownych ścieżkach.

Bandit zgłosił sygnały wymagające interpretacji, nie 33 potwierdzone podatności. Sprawdzono jego kategorie: flagi `0o755` dotyczą pliku wykonywalnego i skryptu dispatchera, URL ma sprawdzany schemat HTTPS, ścieżka X11 jest katalogiem socketów, ścieżki `/tmp` obejmują fallback developerski, a rzekome hasło `custom` jest tokenem trybu EPP. Wywołania subprocess używają argumentów bez `shell=True`. Obsługa ignorowanych wyjątków wymaga zachowania rozróżnienia pomiędzy niekrytycznym cleanup a potwierdzeniem końcowego stanu. Skaner nie potwierdził dodatkowej ścieżki ataku o wadze high.

CI przypina Actions do SHA, ma `contents: read` i nie zachowuje poświadczeń checkouta. Instalacja npm wyłącza lifecycle scripts. Workflow nie ma zadania dependency audit ani harmonogramu wykrywania nowych advisories; obecne `--no-audit` nie zatrzyma builda po pojawieniu się nowego alertu.

## Kolejność poprawek i prób sprzętowych

1. **Przywracanie stanu:** A1 i A2, wspólny kontrakt oddawania kontroli, przypadki własnych mapowań i niezależnych ustawień CPU.
2. **Widoczne błędy i ponowienia:** A3 i A4, tak aby użytkownik mógł rozpoznać problem ustawień lub modułu i bezpiecznie ponowić operację.
3. **Czas i kontekst:** A5 i A6, kontrola całkowitego czasu sieci i ważności informacji o działającej grze.
4. **Potwierdzenie i utrzymanie builda:** A7 i A8, readback wibracji, odświeżenie lockfile oraz ciągłe raportowanie advisories.
5. **Wydanie:** wersja odróżniająca nowe main, pełne CI i próby na konsoli przed publikacją.

| Próba na Legion Go 2 | Warunek zaliczenia |
| --- | --- |
| CPU Boost/EPP i TDP przy wyłączeniu oraz odinstalowaniu | Niezależny odczyt wszystkich zarządzanych wartości po cleanup; zachowane profile użytkownika |
| Remap z własnym profilem oraz restartem InputPlumber | Default odtwarza baseline wybranego przycisku, obce mapowania pozostają, unload oddaje tylko własne wpisy |
| RGB, wibracje, bateria i gyro | Zapis plus odczyt zwrotny; częściowy błąd i hotplug nie zostawiają nieopisanego stanu |
| Sleep/wake, odpięcie kontrolerów, zamknięcie gry i utrata frontendu | Właściwy profil po odzyskaniu, brak nieaktualnego kontekstu i nadmiarowych workerów |
| Preferencja i strict WiFi na iwd | Potwierdzona częstotliwość, rollback po utracie łączności, zachowanie zmian zewnętrznych, sprawny watchdog |
| OLED bez native oraz z native gamescope | Poprawny setup/handoff, zachowanie obcego Lua, wymagany restart i niezależna korekta EDID |
| Zabicie procesu podczas transakcji i niepełny cleanup | Journal pozwala wznowić lub oddać kontrolę, brak startu modułu przed zakończeniem wymaganego recovery |
| Rzeczywisty Steam CEF i kontroler | Nawigacja, focus, dropdowny, komunikaty błędu, ukrycie QAM i powrót bez utraty edycji |
| Powolna sieć i długi reconnect | Bateria, statusy i pozostałe kontrolki pozostają użyteczne; zamykanie mieści się w budżecie Decky |

Próby wymagające zapisu na urządzeniu stanowią dalszą walidację. W tym audycie konsola nie była połączona i nie wykonano żadnych zmian sprzętowych.

## Materiał do odtworzenia

- [Wynik backendu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-backend-tests.log>) oraz [wynik frontendu](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-frontend-tests.log>).
- [Próby backendu A1 A2 A3 A5 A6 A7](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit_legiongo2_backend_probes.py>) i [ich wynik JSON](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-backend-probes.json>).
- [Próby pierwszego błędu frontendu A4](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit_legiongo2_frontend_probes.cjs>) i [ich wynik JSON](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-frontend-probes.json>).
- [Weryfikacja ZIP dokładnym skryptem CI](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit_legiongo2_archive.py>).
- [Pełny npm audit](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-npm.json>) oraz [npm audit z pominięciem devDependencies](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-npm-production.json>).
- [Surowe wyniki Bandit](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-bandit.json>) i [coverage dla własnych modułów](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-coverage.json>).
- [Porównanie vendored pyudev](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit_legiongo2_vendor.py>) i [wynik porównania](<C:/Users/Rayek/Desktop/Codex/Decky Plugins/audit-vendor.json>).

Uruchomienie prób backendu wymaga Pythona i checkoutu we wskazanym katalogu; sprzętowe operacje są zastąpione atrapami. Próby frontendu korzystają z zainstalowanego TypeScript oraz harnessu z repozytorium. Raport i próby nie zostały wysłane do GitHub.
