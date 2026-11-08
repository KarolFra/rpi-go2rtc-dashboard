# rpi-go2rtc-dashboard

Hobbystyczny projekt smart home budowany z ciekawości i dla frajdy na potrzeby prawdziwego domu. Rozpoczął się jako panel kamer na Raspberry Pi z nginx, go2rtc i licznikiem ruchu, a następnie został rozbudowany o wizualizację rolet oraz podgląd stanów centrali SATEL.

![Panel kamer z rozmytymi obrazami](assets/kamery-rozmyte.png)

Archiwalny widok kamer jest celowo rozmyty. Prezentowane wartości licznika nie są aktualne.

## Rozbudowa smart home

- Podgląd domu 3D i sterowania roletami Shelly.
- Podgląd wybranych stanów centrali SATEL INTEGRA i historia obserwowanych zmian.
- Podstawowe moduły stanu, historii i powiadomień e-mail do dalszej integracji.

![Wizualizacja rolet 3D](assets/rolety-3d.png)

Model 3D pochodzi z lokalnej kopii projektu. Część nazw zmieniono na potrzeby prezentacji, adresy ukryto, a aktualnych statusów nie pokazano.

![Przykładowy widok centrali alarmowej](assets/dashboard-desktop.png)

Zrzut centrali przedstawia fikcyjne dane. [Zobacz również widok mobilny](assets/dashboard-mobile.png).

Projekt jest nieoficjalny i nie jest powiązany z firmą SATEL.

### Co jest dostępne w repozytorium

Oryginalny panel kamer i licznik ruchu pozostają w katalogach `web`, `go2rtc`, `systemd` i `scripts`. Dodatkowe pliki w `scripts/satel` to moduły stanu, historii SQLite i powiadomień oraz demo. Nie zawierają komunikacji RS-485 ani USB-RS/RS-232, czytnika centrali ani kompletnego interfejsu rolet i alarmu. Do działania z prawdziwą centralą potrzebny jest osobny adapter i integracja z aplikacją.

Funkcje rolet i panelu SATEL przedstawiają stan prywatnego projektu; z samego tego repozytorium nie da się ich jeszcze zainstalować jako kompletnej aplikacji.

### Uruchomienie dema SATEL

Python 3.10 lub nowszy; bez dodatkowych bibliotek:

```bash
python scripts/satel/demo.py
```

Demo korzysta z fikcyjnych danych oraz tymczasowej bazy i nie łączy się z centralą. Testy, z atrapą serwera pocztowego:

```bash
cd scripts/satel
python -m unittest discover -s ../../tests/satel -v
```

Mapowanie wejść w modułach jest przykładowe i trzeba dostosować je do instalacji. E-mail wymaga osobnej konfiguracji SMTP. Nie zapisuj haseł ani baz z danymi w repozytorium.

### Ograniczenia

Historia obejmuje tylko zmiany zaobserwowane przez aplikację. Krótkie naruszenia i zdarzenia w czasie przerwy w odczycie mogą zostać pominięte. Fizyczne testy oraz wznowienie odczytu po restarcie centrali wymagają dalszej weryfikacji. Powiadomienie o naruszeniu podczas czuwania wymaga wiarygodnego odczytu czuwania odpowiedniej strefy, którego obecnie nie zapewnia publikowany kod. Projekt nie zastępuje systemu alarmowego.

Licznik `usage_monthly.sh` sumuje odczyty RX i TX wskazanego interfejsu na Raspberry Pi (domyślnie `tailscale0`). Nie musi odpowiadać zużyciu całej karty SIM w routerze; po wyzerowaniu liczników interfejsu może również wymagać weryfikacji.

## What's included
- `go2rtc` as an RTSP/WebRTC proxy (with `on_demand` streams)
- `nginx` serves `web/index.html` and `usage.json`
- `usage_monthly.sh` writes monthly usage data to `/var/www/html/usage.json`
- `systemd` timer runs the usage counter every minute

## Repository structure
```text
rpi-go2rtc-dashboard/
|- README.md
|- web/
|  |- index.html
|  `- usage.json.example
|- scripts/
|  |- usage_monthly.sh
|  `- satel/              # moduły pomocnicze i demo offline
|- tests/satel/           # testy modułów SATEL
|- systemd/
|  |- usage-counter.service
|  |- usage-counter.timer
|  `- go2rtc.service.example
`- go2rtc/
   `- go2rtc.yaml.example
```

## Installation (short)
1. Copy `web/index.html` to:
   - `/var/www/html/index.html`
2. Copy `scripts/usage_monthly.sh` to:
   - `/usr/local/bin/usage_monthly.sh`
   - `sudo chmod +x /usr/local/bin/usage_monthly.sh`
3. Copy `systemd/usage-counter.*` to:
   - `/etc/systemd/system/`
4. Enable timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now usage-counter.timer
```

5. go2rtc:
   - config: `/etc/go2rtc/go2rtc.yaml` (see `go2rtc/go2rtc.yaml.example`)
   - service: `systemd/go2rtc.service.example`

Tailscale was used to tunnel VPN access.
