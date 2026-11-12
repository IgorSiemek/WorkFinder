# WorkFinder

WorkFinder wyszukuje staże, praktyki i programy studenckie w Krakowie. Prywatny bot Telegram pokazuje nowe oferty, ocenia dopasowanie i zapisuje historię rekrutacji w SQLite.

Uruchomienie wymaga Pythona 3.12+, tokena bota utworzonego przez oficjalnego [BotFather](https://t.me/BotFather) oraz identyfikatora Twojego konta Telegram. Proces bota korzysta z long pollingu i obsługuje wiadomości oraz harmonogram podczas pracy komputera lub serwera.

1. Zainstaluj zależności w środowisku projektu.

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Utwórz `.env` na podstawie `.env.example`, jeśli plik jeszcze nie istnieje. Ustaw `TELEGRAM_BOT_TOKEN`. Wyślij `/start` do nowego bota i odczytaj swój identyfikator.

   ```powershell
   .venv\Scripts\python.exe -m app.cli telegram-id
   ```

   Wstaw uzyskany numer do `TELEGRAM_USER_ID`. Polecenie odczytuje numery prywatnych rozmów z botem. Uruchamiaj je przy zatrzymanym procesie bota.

3. Przygotuj bazę i konfigurację.

   ```powershell
   .venv\Scripts\python.exe -m app.cli init
   ```

   Pierwsze uruchomienie zapisuje istniejący profil, wyszukiwania, firmy, reguły ignorowania i wersje CV w `data/config.json`. Migracja tworzy backup i dodaje tabele bota. Dla nowej bazy tworzy domyślny profil i ustawienia. Dane demonstracyjne można dodać osobnym poleceniem CLI.

4. Uruchom bota przez `run.bat`, `./run.ps1` lub poniższe polecenie.

   ```powershell
   .venv\Scripts\python.exe -m app.main
   ```

   Wyślij `/start`, następnie wybierz „Szukaj teraz”. Domyślny harmonogram wyszukuje o 09:00 i 17:00 według `Europe/Warsaw`. Przypomnienia o zaległych działaniach pojawiają się raz dziennie po 10:00. Próg dopasowania wynosi 60/100. Te wartości można zmienić w konfiguracji.

Karty ofert mają przyciski otwarcia oferty, zapisania, pominięcia i oznaczenia wysłanej aplikacji. Przycisk „Status” pozwala wybrać etap rekrutacji. Zgłoszenie wysyłasz na stronie pracodawcy, następnie zapisujesz jego status w bocie.

| Polecenie | Działanie |
|---|---|
| `/new`, `/all` | Nowe oferty z 7 dni lub aktywne staże powyżej progu |
| `/saved`, `/applications`, `/ignored` | Zapisane oferty, historia aplikacji, pominięte |
| `/saved 2` | Druga strona listy |
| `/find tekst` | Wyszukiwanie po tytule i firmie, z przyciskami kolejnych stron |
| `/job 12`, `/history 12` | Szczegóły i ostatnie zmiany oferty |
| `/note 12 treść`, `/cv 12 nazwa` | Notatka lub wersja CV, znak `-` usuwa wartość |
| `/date 12 2026-12-01` | Termin następnego działania, znak `-` usuwa termin |
| `/followups`, `/followups 2` | Działania na dziś i zaległe |
| `/search`, `/health` | Wyszukiwanie i stan źródeł oraz kolejki wiadomości |
| `/pause`, `/resume` | Wstrzymanie i wznowienie powiadomień |
| `/config`, `/reload` | Podsumowanie ustawień i wczytanie JSON |
| `/backup`, `/excel`, `/dates 12` | Backup, ponowienie eksportu do Excela, data publikacji |
| `/add Tytuł stażu \| Firma \| https://link \| Hybrid \| Opis` | Ręczne dodanie stażu w Krakowie |
| `/delivery 12` | Ponowienie wskazanej nieudanej wiadomości |
| `/help` | Lista poleceń |
| `/invite`, `/members`, `/revoke ID` | Zaproszenie, lista kont i odebranie dostępu przez administratora |

`data/config.json` można edytować ręcznie lub agentem. `config.example.json` pokazuje ustawienia harmonogramu. Wygenerowany plik zawiera też sekcje `profile`, `searches`, `companies`, `ignore_rules` i `cv_versions`. Pominięta sekcja korzysta z ustawień bazy. Pusta lista wyłącza jej istniejące wpisy. `sources: []` włącza wszystkie dostępne źródła, a lista nazw ogranicza wyszukiwanie do wskazanych providerów. Nazwy znajdziesz pod `/config`.

Po edycji sprawdź plik poleceniem `python -m app.cli check-config`, następnie wyślij `/reload`. Bot zatwierdza konfigurację w całości przed zmianą profilu i przeliczeniem ocen. Gdy trwa wyszukiwanie, ponów `/reload` po jego zakończeniu. Token i identyfikator konta są ustawiane w `.env` i wymagają restartu procesu.

`TELEGRAM_USER_ID` wskazuje administratora bota. Aby udostępnić istniejący profil, oferty i historię aplikacji, administrator wysyła `/invite` i przekazuje otrzymany link drugiej osobie. Na telefonie wystarczy otworzyć link w Telegramie i nacisnąć Start. Zaproszenie jest jednorazowe, ważne 24 godziny, a kolejne `/invite` zastępuje poprzedni link. `/members` pokazuje konta z dostępem, a `/revoke ID` odbiera dostęp wybranej osobie.

Każde konto ma własną rozmowę, kolejkę wiadomości i ustawienie `/pause` lub `/resume`. Zapisane oferty, statusy, notatki, wersje CV i historia rekrutacji są wspólne. Wyszukiwanie według harmonogramu jest wykonywane raz dla całego profilu. Każde konto dostaje własne powiadomienia od chwili dołączenia. Starsze oferty są dostępne przez listy. Odebrane zdarzenia, zmiany aplikacji i odpowiedzi są przechowywane w SQLite z ochroną przed ponownym wykonaniem tego samego polecenia.

Po utracie potwierdzenia wysyłki wiadomość otrzymuje status `uncertain`, widoczny pod `/health`. Możesz sprawdzić rozmowę i ponowić ją przez `/delivery ID`. Ponowienie takiej wiadomości może utworzyć kopię. Błędy połączenia przed wysłaniem i odpowiedzi ograniczające tempo są ponawiane automatycznie.

Po oznaczeniu wysłanej aplikacji proponowany jest follow-up za siedem dni. Jeśli ustawiono `EXCEL_JOBS_PATH`, bot próbuje zsynchronizować status z arkuszem w tle co 30 sekund. Zablokowany arkusz pozostawia zadanie do ponowienia. Import odczytuje istniejący arkusz. Ścieżka do arkusza Windows wymaga dostosowania po przeniesieniu aplikacji na VM.

```powershell
python -m app.cli import-excel
python -m app.cli search
python -m app.cli refresh-dates
python -m app.cli retry-excel
python -m app.cli backup
python -m app.cli export-config
python -m app.cli seed-demo
python -m app.cli remove-demo
python -m pytest
python -m ruff check .
```

`export-config` zapisuje osobny `data/config.export.json`, używając wyłącznego tworzenia pliku. Harmonogram i parametry powiadomień ustawia się w głównym JSON. Spójne kopie bazy są tworzone przed migracją i raz na dobę w `data/backups/`. Zachowywane są wszystkie kopie. Przywrócenie wymaga zatrzymania procesu, zachowania bieżącej bazy i skopiowania wybranego backupu do ścieżki bazy. Backup odtwarza także stan doręczeń z chwili jego utworzenia.

Instrukcja wdrożenia na CX23, konfiguracja GitHub Actions i przeniesienie obecnych danych znajdują się w [docs/hosting.md](docs/hosting.md).

```text
app/
  main.py              uruchomienie bota
  cli.py               zadania administracyjne
  config.py            środowisko i sekrety
  database.py          sesje SQLAlchemy i migracje
  models/              oferty, aplikacje, historia, stan bota i kolejka
  telegram/            API, polecenia, karty, wysyłka i harmonogram
  scrapers/            źródła publicznych ofert
  services/            wyszukiwanie, scoring, konfiguracja JSON, postęp i backupy
alembic/               wersjonowane migracje
```

## Providers and limitations



| Provider | State | Method and limitation |
|---|---|---|
| Arbeitnow | Enabled | Public job-board API, filtered locally for job relevance and Kraków. Coverage depends on its feed. |
| BBH, Aptiv, State Street and Motorola Solutions | Enabled | Their official public Workday search endpoints, queried for Kraków and restricted to internship/training roles. |
| Shell, ABB and Aon | Manual watchlist | Official careers links are available on Companies; no automatic provider is claimed. |
| Hitachi | Enabled | Structured public Kraków results, including later pages and roles whose experience category is Entry Level. |
| PMI and Cisco | Enabled | Structured public Phenom search with pagination and Polish/English internship queries. City is checked on each offer; future talent pools are excluded. |
| Alior | Enabled | Paginated official offer table; city and publication date come from the same row. |
| UBS, HID, HSBC, Capgemini and HEINEKEN | Limited | Conservative single-page HTML monitors. JavaScript-only pages and access blocks can prevent discovery. An empty parse is Unverified, not proof that no offers exist. |
| LinkedIn Jobs | Stub | No login scraping. LinkedIn can change markup and restrict automated access. Add jobs manually. |
| Pracuj.pl / Talent Days | Stub | No stable public API has been assumed. A dedicated adapter should be added only after confirming permitted public access. |
| EY, PwC, Deloitte, KPMG, Accenture | Stub | Career platforms differ by region and change vendors. Manual import remains available. |
| Banks and institutions in the requested watchlist | Stub | Stored as watched companies, without brittle HTML scrapers. |

A provider failure never stops other providers. Providers are fetched concurrently, so a
slow careers site does not hold up every other source. Telegram /health shows the latest provider states. Search runs and per-offer decisions are stored in SQLite. Empty responses from conservative monitors are marked **Unverified**
and cannot expire old jobs. Incomplete pagination is marked **Partial**. Complete
structured Workday, Hitachi or Alior scans can expire a job after seven days without
being seen; truncated scans, keyword-only searches and first-page feeds cannot do so.
All jobs and application history remain stored.

The live employer audit and sample offers are recorded in
[`docs/provider-validation-2026-10-17.md`](docs/provider-validation-2026-10-17.md).

### Adding a provider

1. Add a class in `app/scrapers/` implementing `JobSource.search_jobs(SearchCriteria)`.
2. Return normalized `JobPosting` objects and plain text descriptions. Use only an official API, RSS feed, documented public endpoint, or stable public structured data.
3. Apply `relevant_to_krakow` so the source cannot broaden the location scope.
4. Register the class in `app/scrapers/registry.py`.
5. Add a parsing test with a saved, minimal response fixture or mocked request.

Do not add credentials for job portals to `.env`. WorkFinder deliberately avoids login-protected acquisition.
