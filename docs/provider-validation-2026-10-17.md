# Kontrola źródeł pracodawców — 17.10.2026

## Wynik rzeczywistego wyszukiwania

Uruchomiono pełne wyszukiwanie na lokalnej bazie po wykonaniu kopii zapasowej.
Przebieg nr 20: **31 pasujących ofert, 16 nowych, 15 ponownie znalezionych**.
Jedno ogłoszenie Cisco odrzucono jako nabór na przyszłość. Nie wystąpił błąd
importu ani wyjątek dostawcy. Cztery strony pracodawców pozostały niezweryfikowane;
brak wyjątku nie oznacza pełnego pokrycia tych stron.
Historia 16 aplikacji pozostała w bazie.

| Pracodawca | Pasujące oferty | Wynik kontroli |
|---|---:|---|
| Hitachi | 7 | Wszystkie 153 rekordy krakowskiej listy, 16 stron. Oferty stażowe występują także w kategorii Entry Level, więc filtr wyłącznie Internship je pomija. |
| Philip Morris International | 10 | Publiczne dane ofert i kolejne strony dla polskich i angielskich haseł. Usunięto wadliwy parametr `location=Krakow`, który zwracał zero wyników. |
| Motorola Solutions | 9 | Publiczny Workday; data z `startDate`, rzeczywisty opis i lokalizacje dodatkowe ze szczegółów. |
| BBH | 2 | Publiczny Workday. |
| HID | 2 | Oferty z kart na oficjalnej stronie, lokalizacja w obrębie tej samej karty. |
| Aptiv | 1 | Publiczny Workday. |
| State Street | 0 | Ukończone wyszukiwanie Workday bez pasujących ofert. |
| Alior | 0 | Przejście po stronach tabeli ofert; lokalizacja i data odczytywane z tego samego wiersza. |
| Cisco | 0 + 1 odrzucona | Poprawiono adres na nową stronę kariery. Krakowski program dla techników zawiera informację, że konkretna rola może nie być otwarta; zapisano decyzję `excluded_talent_pool`. |
| UBS | nieustalone | HTML zawiera szablony JavaScript zamiast gotowych ofert. Stan Unverified. |
| HSBC | nieustalone | Obecny monitor nie potwierdził wyników stażowych. Stan Unverified. |
| Capgemini | nieustalone | Dodatkowo sprawdzono wyszukiwanie `intern`, `trainee`, `staż`. Wyniki `intern` zawierały też zwykłe stanowiska, odrzucone po tytule. Stan Unverified. |
| HEINEKEN | nieustalone | Podczas kontroli źródło zwracało również blokadę HTTP 403. Pełny przebieg zakończył monitor bez parsowalnych ofert: Unverified. |

Arbeitnow również sprawdzono; zero pasujących rekordów, bez potwierdzenia pełnego pokrycia.
Liczby są migawką z dnia kontroli, a nie gwarancją kolejnych wyników.

## Ręczne porównanie z ogłoszeniami

Kontrola odbyła się przez odczyt publicznych stron szczegółów i danych osadzonych
przez pracodawców w HTML. Sterowana przeglądarka nie była dostępna w tej sesji;
nie wykonano wizualnego testu interfejsu ani klikania formularzy aplikacyjnych.

Otworzono osobno wszystkie **10 stron PMI i 7 stron Hitachi** znalezionych przez
nowe adaptery. Każdy adres zwrócił HTTP 200. Dla PMI identyfikator, tytuł,
lokalizacja Krakow i data z `jobDetail` odpowiadały rekordom wyszukiwania. Dla
Hitachi nagłówki stron odpowiadały tytułom; daty odczytuje istniejący parser
publikacji ze stron ofert. Przeczytano także treść przykładowej oferty Accounts
Payable oraz informację o przyszłym naborze Cisco.

Przykłady porównane z oficjalnymi stronami:

| Oferta | ID | Data pracodawcy | Lokalizacja |
|---|---|---|---|
| [Hitachi — Internship - Accounts Payable](https://careers.hitachi.com/internship-accounts-payable/job/R0142135) | R0142135 | 2026-10-12 | Kraków, hybrydowo |
| [Hitachi — Staż - Analityk IT](https://careers.hitachi.com/pl/sta%C5%BC-analityk-it/job/R0147178) | R0147178 | 2026-10-15 | Kraków |
| [PMI — Intern in Controlling](https://join.pmicareers.com/gb/en/job/33849/Intern-in-Controlling) | 33849 | 2026-09-30 | Kraków |
| [PMI — Intern in Data & Analytics](https://join.pmicareers.com/gb/en/job/34321/Intern-in-Data-Analytics) | 34321 | 2026-10-02 | Kraków |

Przykład dat w aplikacji: PMI Data & Analytics ma datę publikacji **02.10.2026**
i datę pierwszego znalezienia **17.10.2026**. Są to oddzielne pola.

## Kontrola aplikacji i regresji

- 92 testy automatyczne; dodatkowe przypadki obejmują stronicowanie, powtarzaną
  stronę, brak danych źródła, dodatkowe lokalizacje Workday, daty z wiersza Alior,
  lokalizację na zagnieżdżonej karcie HID i rozpoznawanie naborów na przyszłość.
- Ruff: bez błędów.
- Żądania HTTP do `/`, `/jobs`, `/companies`, `/settings`, `/runs`, `/runs/20`
  oraz szczegółów nowej oferty PMI: HTTP 200. Sprawdzono obecność oferty, statusu
  Limited w ustawieniach oraz decyzji o odrzuceniu naboru Cisco.
- Kopia bazy: `data/backups/workfinder-20261017-141642-596477.sqlite3`.
- Monitory o ograniczonym pokryciu są oznaczone jako Limited. Niepełne skany
  otrzymują Partial, a puste wyniki monitorów — Unverified. Nie mogą powodować
  automatycznego wygaszania historycznych ofert.

Powtórzenie kontroli:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m app.cli search
```

Po wyszukiwaniu otwórz **Search Runs**, sprawdź wyniki poszczególnych źródeł,
następnie przejdź do szczegółów oferty i porównaj ją z linkiem pracodawcy.
