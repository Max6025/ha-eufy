# Eufy Max

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/Max6025/eufy_max/actions/workflows/validate.yml/badge.svg)](https://github.com/Max6025/eufy_max/actions/workflows/validate.yml)

Eigene Home-Assistant-Integration für Eufy-Security-Geräte.


## Was anders ist

Diese Integration setzt auf `eufy-security-ws` als Backend auf (das Eufy-Protokoll
selbst zu implementieren ist nicht sinnvoll), macht darüber aber alles selbst:

**1. Entities werden dynamisch generiert.**
Beim Start werden per `device.get_properties_metadata` alle Eigenschaften jeder
Kamera abgefragt und daraus automatisch Entities gebaut:

| Property-Typ | wird zu |
|---|---|
| boolean + schreibbar | `switch` |
| mit Zustandsliste + schreibbar | `select` |
| number + schreibbar | `number` |
| nur lesbar | `sensor` |
| `light` | `light` |

Das heißt: jede Kamera bekommt genau die Schalter, die sie tatsächlich kann.
Nichts fehlt, nichts ist deaktiviert, nichts muss nachträglich freigeschaltet werden.
Wenn Eufy per Firmware eine neue Einstellung hinzufügt, taucht sie nach einem
Neustart automatisch auf.

**2. Echter Reconnect-Watchdog.**
`websocket.py` hält die Verbindung mit Heartbeat und Backoff dauerhaft am Leben.
Nach einem Abbruch wird endlos neu verbunden (2s → 4s → 8s … max 60s), statt die
Entities dauerhaft auf `unavailable` stehen zu lassen.

**3. Livestream auf Knopfdruck statt dauerhaft.**
Die Kameras streamen nicht permanent. Ein Button startet alle Kameras gleichzeitig,
ein einstellbarer Timer schaltet sie automatisch wieder ab.

| Entity | Zweck |
|---|---|
| `button.eufy_max_steuerung_livestream_starten` | startet alle Kameras |
| `button.eufy_max_steuerung_livestream_stoppen` | stoppt sofort |
| `switch.eufy_max_steuerung_livestream` | Hauptschalter, an = Timer läuft |
| `number.eufy_max_steuerung_livestream_dauer` | Dauer in Sekunden (10–1800) |
| `sensor.eufy_max_steuerung_livestream_restzeit` | Countdown, sekundengenau |
| `sensor.eufy_max_steuerung_livestream_endet_um` | Abschaltzeitpunkt |

Beim Start wird RTSP an jeder Kamera aktiviert und deren eigener Stream genutzt
(P2P nur als Fallback), beim Ablauf wieder deaktiviert. Im Ruhezustand zeigt die
Kamera-Karte das letzte Ereignisbild. Die eingestellte Dauer überlebt einen
Neustart von Home Assistant.

**4. Alarm Panel je Kamera plus Sammelpanel.**
Ohne HomeBase ist jede Kamera ihre eigene Station und hat einen eigenen Guard Mode.
Deshalb bekommt jede Kamera ein `alarm_control_panel`, mit dem sie einzeln scharf
und unscharf geschaltet wird — dazu ein Sammelpanel für alle gleichzeitig.

| Panel-Zustand | Eufy Guard Mode |
|---|---|
| `armed_away` | 0 – Abwesend |
| `armed_home` | 1 – Zuhause |
| `armed_custom_bypass` | 2 – Zeitplan |
| `armed_night` | 3 – eigener Modus 1 |
| `armed_vacation` | 4 – eigener Modus 2 |
| `disarmed` | 63 – Unscharf |
| `triggered` | Sirene läuft |

Hat eine Kamera keine eigene Station, schaltet das Panel ersatzweise die
Bewegungserkennung. Die eigenen Modi 1–3 sind die, die du in der Eufy-App selbst
angelegt hast (Reihenfolge nach Erstellungsdatum).

**5. Captcha und 2FA im UI.**
Fordert Eufy ein Captcha oder einen Code an, kommt eine Benachrichtigung mit der
ID. Antwort per Service `eufy_max.set_captcha` bzw. `eufy_max.set_verify_code`.

## Voraussetzung

Add-on `eufy-security-ws` installieren und mit dem **Owner-Account** konfigurieren.
Add-on-Repository in Home Assistant hinzufügen:

```
https://github.com/bropat/hassio-eufy-security-ws
```

## Installation über HACS

1. HACS öffnen → oben rechts die drei Punkte → **Benutzerdefinierte Repositories**
2. URL `https://github.com/Max6025/eufy_max` eintragen, Kategorie **Integration**
3. „Eufy Max" suchen und herunterladen
4. Home Assistant neu starten
5. Einstellungen → Geräte & Dienste → Integration hinzufügen → **Eufy Max**
6. Host `127.0.0.1`, Port `3000`

## Installation von Hand

Ordner `custom_components/eufy_max` nach `/config/custom_components/eufy_max/`
kopieren, Home Assistant neu starten, dann wie oben ab Schritt 5.

> Die bestehende `fuatakgun/eufy_security` Integration vorher entfernen — zwei
> Clients auf demselben WS-Server vertragen sich nicht.

## Services

| Service | Zweck |
|---|---|
| `eufy_max.set_captcha` | Captcha-Lösung senden |
| `eufy_max.set_verify_code` | 2FA-Code senden |
| `eufy_max.set_property` | beliebige Eigenschaft direkt setzen |
| `eufy_max.ptz` | schwenken/neigen (`left`, `right`, `up`, `down`, `rotate360`) |
| `eufy_max.reconnect` | Cloud-Verbindung neu aufbauen |
| `eufy_max.start_stream` | Livestream starten, optional mit `duration` in Sekunden |
| `eufy_max.stop_stream` | Livestream sofort stoppen |
| `eufy_max.set_guard_mode` | Guard Mode direkt setzen, ohne Ziel für alle |

## Beispiel-Automatisierung

```yaml
- id: eufy_veranda_licht_bei_person
  alias: Veranda Licht bei Person
  trigger:
    - platform: state
      entity_id: binary_sensor.veranda_person
      to: "on"
  condition:
    - condition: sun
      after: sunset
  action:
    - service: light.turn_on
      target:
        entity_id: light.veranda_licht
      data:
        brightness: 255
    - delay:
        minutes: 3
    - service: light.turn_off
      target:
        entity_id: light.veranda_licht
  mode: restart
```

## Beispiel: Stream bei Bewegung automatisch starten

```yaml
- id: eufy_stream_bei_bewegung
  alias: Livestream bei Bewegung
  trigger:
    - platform: state
      entity_id: binary_sensor.vordertuer_person
      to: "on"
  action:
    - service: eufy_max.start_stream
      data:
        duration: 180
  mode: restart
```

## Beispiel: nachts alles scharf

```yaml
- id: eufy_nachts_scharf
  alias: Kameras nachts scharf
  trigger:
    - platform: sun
      event: sunset
      offset: "00:30:00"
  action:
    - service: alarm_control_panel.alarm_arm_away
      target:
        entity_id: alarm_control_panel.eufy_max_steuerung_alarm_alle_kameras
  mode: single

- id: eufy_wohnzimmer_unscharf_wenn_daheim
  alias: Wohnzimmer unscharf wenn jemand da ist
  trigger:
    - platform: state
      entity_id: person.max
      to: "home"
  action:
    - service: alarm_control_panel.alarm_disarm
      target:
        entity_id: alarm_control_panel.wohnzimmer_alarm
  mode: single
```

## Fehlersuche

Debug-Log in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.eufy_max: debug
```

Häufigste Ursache für fehlende Schalter: der eingetragene Account ist ein
Gast-Account. Freigegebene Eufy-Accounts dürfen viele Eigenschaften nicht
schreiben — dann liefert `get_properties_metadata` sie als nur lesbar, und sie
werden zu Sensoren statt zu Schaltern.
