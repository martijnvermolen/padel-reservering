# Automatische Padelbaan Reservering

Automatisch een padelbaan reserveren via het KNLTB reserveringssysteem (directe HTTP API, geen browser nodig).

## Wat doet deze applicatie?

- Logt automatisch in op het KNLTB reserveringssysteem
- Reserveert een padelbaan op je gewenste dag(en) en tijd(en)
- Voegt medespelers toe aan de reservering
- **Retry-loop**: begint 3 minuten voor de 48-uur grens en probeert elke 10 seconden
- Stuurt een e-mailnotificatie bij succes of falen
- Draait automatisch via **crontab op een Raspberry Pi**
- Configuratie aanpasbaar via telefoon (PWA dashboard)

## Raspberry Pi deployment (primair)

De bot draait op een Raspberry Pi met lokale crontab voor precieze timing.

### Installatie

```bash
cd ~/reservering
pip3 install -r requirements.txt
```

### Credentials instellen

```bash
cp .env.example .env
nano .env  # Vul KNLTB_USERNAME, KNLTB_PASSWORD en EMAIL_PASSWORD in
```

### Crontab instellen

```bash
python3 setup_cron.py
```

Dit genereert automatisch crontab-entries op basis van `config.yaml`:
- 3 minuten voor elk 48u-venster opent
- Elk uur een sync van config.yaml vanuit GitHub

### Logbestanden

Output van de bot wordt gelogd naar `~/reservering/cron.log`.

## GitHub Actions (noodknop)

De GHA workflow is beschikbaar als handmatige trigger via **Actions** > **Padel Reservering** > **Run workflow**. Automatische cron is uitgeschakeld (de Pi is primair).

### GitHub Secrets

| Secret           | Waarde                              |
|-----------------|-------------------------------------|
| `KNLTB_USERNAME` | Je bondsnummer (bijv. `30783682`)   |
| `KNLTB_PASSWORD` | Je KNLTB wachtwoord                 |
| `EMAIL_PASSWORD` | Gmail App Password (optioneel)      |

## Lokaal draaien

```bash
# Automatisch (bepaalt zelf welke dag aan de beurt is)
python main.py

# Voor een specifieke dag (0=ma, 1=di, ..., 6=zo)
python main.py --dag 1

# Dry-run (simuleert zonder daadwerkelijk te reserveren)
python main.py --dry-run

# Zonder retry-loop (eenmalige poging)
python main.py --no-retry

# Verbose logging
python main.py --verbose
```

## Configuratie

Bewerk `config.yaml` (of via het PWA dashboard op je telefoon):

- **reservering.dagen** - Op welke dagen en tijden je wilt spelen
- **reservering.baan_voorkeur** - Voorkeursbaan (standaard baan 1)
- **medespelers.spelers_per_dag** - Medespelers per dag
- **email** - E-mailinstellingen voor notificaties

## Problemen oplossen

| Probleem | Oplossing |
|---|---|
| Login mislukt | Controleer KNLTB_USERNAME en KNLTB_PASSWORD in `.env` |
| Geen beschikbare baan | Alle voorkeurtijden zijn bezet. Voeg meer fallback-tijden toe |
| E-mail wordt niet verstuurd | Controleer EMAIL_PASSWORD. Voor Gmail: maak een App Password |
| Retry timeout | Banen waren al bezet voordat de bot ze kon pakken |
| Pi log bekijken | `cat ~/reservering/cron.log` |
