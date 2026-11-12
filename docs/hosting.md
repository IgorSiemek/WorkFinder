# Wdrożenie WorkFinder na Hetzner CX23

CX23 z 2 vCPU, 4 GB RAM i 40 GB NVMe jest punktem startowym dla jednego użytkownika, SQLite i okresowego pobierania ofert. Wielkość instancji dobrano do architektury aplikacji. Zużycie pamięci podczas wyszukiwania warto sprawdzić po uruchomieniu przez `docker stats`. Parametry instancji podaje [Hetzner](https://www.hetzner.com/cloud/cost-optimized/).

Docelowy układ to Ubuntu 24.04 LTS, Docker Engine z Compose v2 oraz jeden kontener `workfinder`. SQLite, JSON i backupy znajdują się w katalogu `data` podłączonym z VM. Bot nawiązuje połączenia HTTPS do Telegrama i źródeł ofert. Administracja odbywa się przez SSH. Compose sprawdza stan procesu na podstawie czasu ostatniego udanego pobrania aktualizacji z Telegrama i identyfikatora wersji obrazu.

Workflow `.github/workflows/ci.yml` używa schematu z repozytorium Pomarancze. GitHub Actions wykonuje Ruff, pytest, budowę obrazu i inicjalizację czystej bazy w kontenerze. Następnie łączy się przez SSH, pobiera przetestowany commit `main`, buduje obraz na VM i zastępuje kontener. Wdrożenie czeka na stan `healthy` i sprawdza etykietę commita obrazu. Kolejne wdrożenia są wykonywane po kolei.

1. Przygotuj CX23 z Ubuntu 24.04 LTS. Zainstaluj Git, Docker Engine i wtyczkę Compose zgodnie z [instrukcją Dockera dla Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Konto wdrożeniowe musi obsługiwać Docker przez SSH.

2. Przygotuj katalog i klon repozytorium na VM. Dla repozytorium prywatnego skonfiguruj na VM dostęp do odczytu GitHub, np. repozytoryjny deploy key.

   ```bash
   sudo mkdir -p /opt/workfinder
   sudo chown "$USER:$USER" /opt/workfinder
   git clone git@github.com:IgorSiemek/WorkFinder.git /opt/workfinder
   cd /opt/workfinder
   mkdir -p data
   cp .env.example .env
   ```

3. W pliku `.env` na VM ustaw `TELEGRAM_BOT_TOKEN` i `TELEGRAM_USER_ID`. Są to ustawienia środowiska kontenera. Compose ustawia ścieżki bazy, JSON i backupów w `/app/data`. Jeśli chcesz synchronizować Excel, skopiuj arkusz do `data` i ustaw ścieżkę kontenera, np. `EXCEL_JOBS_PATH=/app/data/jobs.xlsx`. Dla pracy bez arkusza ustaw pustą wartość.

4. Przenieś obecną bazę, konfigurację i wybrane backupy z komputera do `/opt/workfinder/data`. Zatrzymaj lokalny proces bota na czas kopiowania bazy. Podstawowe pliki to `data/workfinder.db` i `data/config.json`. Pierwsze uruchomienie na VM wykona brakujące migracje i utworzy backup.

   Po skopiowaniu ustaw właściciela katalogu danych na UID kontenera.

   ```bash
   cd /opt/workfinder
   sudo chown -R 10001:10001 /opt/workfinder/data
   chmod 600 .env
   ```

   Agent edytujący JSON na serwerze potrzebuje uprawnień zapisu do tego pliku. Edycję administracyjną można wykonać przez `sudoedit /opt/workfinder/data/config.json`, a następnie zastosować przez `/reload`.

5. W repozytorium WorkFinder skonfiguruj GitHub Environment `production`, z nazwami stosowanymi w Pomaranczach.

   | Nazwa | Rodzaj | Wartość |
   |---|---|---|
   | `PROD_VM_HOST` | Variable lub Secret | Adres CX23 |
   | `PROD_VM_USER` | Variable lub Secret | Użytkownik SSH z dostępem do Dockera |
   | `PROD_VM_SSH_PORT` | Variable lub Secret, opcjonalnie | Domyślnie `22` |
   | `PROD_VM_APP_DIR` | Variable, opcjonalnie | Domyślnie `/opt/workfinder` |
   | `PROD_VM_SSH_PRIVATE_KEY` | Secret | Klucz SSH konta wdrożeniowego |

   Sekrety i zmienne GitHub są przypisane do repozytorium lub Environment. W WorkFinder trzeba je ustawić dla docelowej VM. Runtime bota pobiera token z `.env` na serwerze.

6. Push na `main` uruchamia testy i automatyczny deploy. Można również uruchomić workflow ręcznie z `main`. `/start` w Telegramie aktywuje obserwację nowych ofert i harmonogram. Tylko jedna instancja może pobierać aktualizacje dla danego tokena. Instancję lokalną zatrzymaj przed uruchomieniem tej samej konfiguracji na VM.

Podstawowe polecenia administracyjne na VM są następujące.

```bash
cd /opt/workfinder
docker compose ps
docker compose logs --tail=100 -f workfinder
docker compose exec -T workfinder python -m app.cli healthcheck
docker compose exec -T workfinder python -m app.cli backup
docker compose stop
docker compose up -d --wait --wait-timeout 150
```

Workflow oznacza obrazy pełnym SHA commita. Po automatycznym wdrożeniu przy ręcznych poleceniach Compose ustaw `APP_BUILD_REF` zgodnie z wersją obrazu.

```bash
export APP_BUILD_REF="$(git rev-parse HEAD)"
```

Pliki `data/config.json` i `.env` mają własny cykl zmian. Kod źródłowy na VM jest aktualizowany przez workflow. Baza jest podłączona z katalogu hosta, więc odtworzenie kontenera korzysta z danych tej VM.

Automatyczny backup SQLite powstaje w `data/backups`. Przenoś wybrane spójne pliki `.sqlite3` oraz JSON poza VM. Backup całej maszyny Hetznera stanowi dodatkową kopię. Hetzner utrzymuje siedem codziennych backupów serwera. [Dokumentacja backupów Hetznera](https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/)

Przy odtwarzaniu zatrzymaj kontener, zachowaj bieżący `workfinder.db`, skopiuj wybrany backup pod tę nazwę, sprawdź właściciela UID 10001 i uruchom Compose. Stan kolejki wiadomości oraz odebranych aktualizacji wraca do chwili backupu, co może spowodować ponowne powiadomienia.
