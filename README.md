# Automatische Padelbaan Reservering - TPV Heksenwiel

Automatisch een padelbaan reserveren bij TPV Heksenwiel via het KNLTB reserveringssysteem.

## Wat doet deze applicatie?

- Logt automatisch in op het KNLTB reserveringssysteem van TPV Heksenwiel
- Reserveert een padelbaan op je gewenste dag(en) en tijd(en)
- Voegt medespelers toe aan de reservering
- **Retry-loop**: begint 5 minuten voor de 48-uur grens en probeert elke 10 seconden
- Stuurt een e-mailnotificatie bij succes of falen
- Draait automatisch via **GitHub Actions** (gratis, geen laptop nodig)

## Cloud deployment (GitHub Actions)

De applicatie draait volledig automatisch in de cloud via GitHub Actions.

### Schedule

| Reservering        | GitHub Actions start          | Retry-loop               |
|-------------------|-------------------------------|--------------------------|
| Dinsdag 20:30     | Zondag 20:25 (NL tijd)        | 20:25 - 20:32            |
| Zondag 19:30      | Vrijdag 19:25 (NL tijd)       | 19:25 - 19:32            |

### GitHub Secrets instellen

Ga naar je repository > **Settings** > **Secrets and variables** > **Actions** en voeg toe:

| Secret           | Waarde                              |
|-----------------|-------------------------------------|
| `KNLTB_USERNAME` | Je bondsnummer (bijv. `30783682`)   |
| `KNLTB_PASSWORD` | Je KNLTB wachtwoord                 |
| `EMAIL_PASSWORD` | Gmail App Password (optioneel)      |

### Handmatig triggeren

Je kunt de workflow ook handmatig starten via **Actions** > **Padel Reservering** > **Run workflow**.

## Lokaal draaien

### Installatie

```powershell
pip install -r requirements.txt
playwright install chromium
```

### Wachtwoorden instellen

```powershell
# Environment variables (aanbevolen)
[System.Environment]::SetEnvironmentVariable("KNLTB_USERNAME", "30783682", "User")
[System.Environment]::SetEnvironmentVariable("KNLTB_PASSWORD", "jouw_wachtwoord", "User")
[System.Environment]::SetEnvironmentVariable("EMAIL_PASSWORD", "jouw_email_wachtwoord", "User")
```

### Gebruik

```powershell
# Automatisch (bepaalt zelf welke dag aan de beurt is)
python main.py

# Met zichtbare browser (voor testen)
python main.py --visible

# Voor een specifieke dag (0=ma, 1=di, ..., 6=zo)
python main.py --dag 1

# Zonder retry-loop (eenmalige poging)
python main.py --no-retry

# Dry-run (simuleert zonder daadwerkelijk te reserveren)
python main.py --dry-run

# Combinatie: test zichtbaar zonder retry
python main.py --visible --no-retry --dry-run
```

## Configuratie

Bewerk `config.yaml` voor:

- **reservering.dagen** - Op welke dagen en tijden je wilt spelen
- **reservering.baan_voorkeur** - Voorkeursbaan (standaard baan 1)
- **medespelers.spelers_per_dag** - Medespelers per dag
- **email** - E-mailinstellingen voor notificaties

## Logbestanden en screenshots

- **reservering.log** - Alle activiteit wordt gelogd
- **screenshots/** - Screenshots van elke stap (als artifact beschikbaar in GitHub Actions)

## Problemen oplossen

| Probleem | Oplossing |
|---|---|
| Login mislukt | Controleer KNLTB_USERNAME en KNLTB_PASSWORD secrets |
| Geen beschikbare baan | Alle voorkeurtijden zijn bezet. Voeg meer fallback-tijden toe |
| E-mail wordt niet verstuurd | Controleer EMAIL_PASSWORD. Voor Gmail: maak een App Password |
| Retry timeout | Banen waren al bezet voordat de bot ze kon pakken |
