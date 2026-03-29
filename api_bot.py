"""
api_bot.py - Directe HTTP-gebaseerde reservering voor KNLTB baanreservering.

Vervangt de Playwright browser-automatisering door directe HTTP-calls.
Dezelfde interface als ReserveringBot zodat main.py het als drop-in kan gebruiken.

Flow:
  1. POST /mijn                          -> Login (session cookies)
  2. POST /Ajax/Profile/AddPlayer?id=... -> Spelers toevoegen (per speler)
  3. POST /me/ReservationsPlayersPost    -> Spelers bevestigen
  4. POST /me/ReservationsDay            -> Dag + dagdeel selecteren
  5. POST /me/ReservationsCourt          -> Baan + tijd selecteren
  6. POST /me/ReservationsConfirm        -> Reservering bevestigen
"""

import html as html_mod
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NL_TZ = ZoneInfo("Europe/Amsterdam")

BASE_URL = "https://tpv-heksenwiel.knltb.site"

PADEL_COURTS = {
    1: "27247a4e-0443-411a-be10-ba08ccd40cde",
    2: "3920ba34-a11e-4108-ae84-b51d469659ac",
    3: "4ce24896-1ca0-48cd-8c53-2d7ccf354b84",
    4: "47acc23a-8c78-4316-8f70-b052c95ea910",
}

PADEL_COURT_NAMES = {v: f"Padel {k}" for k, v in PADEL_COURTS.items()}


class ReserveringError(Exception):
    pass


class ApiReserveringBot:
    """Directe HTTP-gebaseerde padelbaan reservering via KNLTB.site."""

    def __init__(self, config: dict, verbose_screenshots: bool = False, label: str = ""):
        self.config = config
        self.label = label
        self._log = logging.getLogger(f"{__name__}.{label}" if label else __name__)
        self._session: requests.Session | None = None
        self._speler_guids: dict[str, str] = {}
        self._laatste_fout: str | None = None

    def start(self):
        """Start een HTTP-sessie en log in."""
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        self._login()

    def stop(self):
        """Sluit de HTTP-sessie."""
        if self._session:
            self._session.close()
            self._session = None
        self._log.info("Sessie gesloten")

    def _login(self):
        creds = self.config["credentials"]
        username = os.environ.get("KNLTB_USERNAME", "") or creds.get("username", "")
        password = os.environ.get("KNLTB_PASSWORD", "") or creds.get("password", "")
        if not username or not password:
            raise ReserveringError("Geen credentials geconfigureerd")

        self._log.info(f"Inloggen als {username}...")
        resp = self._session.post(f"{BASE_URL}/mijn", data={
            "Login.LoginType": "FedmembershipNumber",
            "Login.MembershipNumber": username,
            "Login.Password": password,
        }, allow_redirects=True)

        page_lower = resp.text.lower()

        if resp.status_code != 200:
            raise ReserveringError(f"Login mislukt (HTTP {resp.status_code})")

        # Detecteer of het login-formulier nog zichtbaar is (= login mislukt)
        login_form_indicators = ['type="password"', "type='password'", 'login.password']
        if any(indicator in page_lower for indicator in login_form_indicators):
            self._log.error(f"Login-formulier nog zichtbaar na POST - credentials incorrect?")
            self._log.debug(f"Response URL: {resp.url}")
            raise ReserveringError("Login mislukt - login-formulier nog zichtbaar (controleer credentials)")

        if len(self._session.cookies) == 0:
            raise ReserveringError("Login mislukt - geen sessie-cookies ontvangen")

        self._log.info(f"Login geslaagd ({len(self._session.cookies)} cookies, URL: {resp.url})")

    def _get_csrf(self, html: str) -> str:
        match = re.search(r'__RequestVerificationToken.*?value="([^"]+)"', html)
        if not match:
            raise ReserveringError("CSRF token niet gevonden")
        return match.group(1)

    def _parse_speler_cards(self, html: str) -> dict[str, str]:
        """
        Parse HTML met addPlayer cards om naam->GUID mapping te extraheren.

        De cards hebben dit formaat:
          <div class="card-body addPlayer" ... data-id="GUID">
              <img ...>
              Naam
              <a ...>...</a>
          </div>
        """
        guids = {}
        for m in re.finditer(
            r'class="card-body\s+addPlayer"[^>]*data-id="([a-f0-9-]+)"[^>]*>(.*?)</div>',
            html,
            re.DOTALL,
        ):
            guid = m.group(1)
            inner = re.sub(r'<[^>]+>', ' ', m.group(2))
            inner = html_mod.unescape(re.sub(r'\s+', ' ', inner).strip())
            if inner:
                guids[inner] = guid

        return guids

    def _ontdek_speler_guids(self) -> dict[str, str]:
        """Parse de ReservationsPlayers pagina om recente speler GUIDs te vinden."""
        resp = self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
        if resp.status_code != 200:
            raise ReserveringError(f"Kon spelers-pagina niet laden (status {resp.status_code})")

        guids = self._parse_speler_cards(resp.text)

        self._log.info(f"Recente speler GUIDs gevonden: {len(guids)}")
        for naam, guid in guids.items():
            self._log.debug(f"  {naam} = {guid}")

        return guids

    def _zoek_spelers(self, zoekterm: str) -> dict[str, str]:
        """Zoek spelers via de AJAX search-endpoint met een zoekterm."""
        resp = self._session.get(
            f"{BASE_URL}/Ajax/Profile/SearchPlayers",
            params={"term": zoekterm},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if resp.status_code != 200:
            self._log.warning(f"Spelers zoeken mislukt (status {resp.status_code})")
            return {}

        return self._parse_speler_cards(resp.text)

    def _zoek_alle_spelers(self) -> dict[str, str]:
        """
        Haal alle clubleden op door per letter a-z te zoeken.
        De API retourneert max 20 resultaten per zoekterm; door elke letter
        apart te zoeken bereiken we goede dekking van het ledenbestand.
        """
        import string

        alle_spelers: dict[str, str] = {}
        for letter in string.ascii_lowercase:
            resultaten = self._zoek_spelers(letter)
            nieuwe = {k: v for k, v in resultaten.items() if k not in alle_spelers}
            alle_spelers.update(resultaten)

            if resultaten and nieuwe:
                self._log.debug(
                    f"  Zoek '{letter}': {len(resultaten)} gevonden, "
                    f"{len(nieuwe)} nieuw (totaal: {len(alle_spelers)})"
                )

        self._log.info(f"Alle spelers via search API: {len(alle_spelers)}")
        return alle_spelers

    def _voeg_spelers_toe(self, spelers: list[str]) -> int:
        """Voeg spelers toe via AJAX en return het aantal succesvol toegevoegde."""
        if not self._speler_guids:
            self._speler_guids = self._ontdek_speler_guids()

        toegevoegd = 0
        for speler in spelers:
            guid = self._speler_guids.get(speler)
            if not guid:
                # Zoek op achternaam in bekende GUIDs
                achternaam = speler.split()[-1].lower()
                for naam, g in self._speler_guids.items():
                    if achternaam in naam.lower():
                        guid = g
                        break

            if not guid:
                # Speler niet in recente lijst, zoek via search API
                self._log.info(f"Speler '{speler}' niet in recente lijst, zoek via API...")
                zoek_resultaten = self._zoek_spelers(speler)
                if zoek_resultaten:
                    guid = next(iter(zoek_resultaten.values()))
                    naam = next(iter(zoek_resultaten.keys()))
                    self._speler_guids[naam] = guid
                    self._log.info(f"Gevonden via search: {naam} ({guid[:8]}...)")

            if not guid:
                self._log.warning(f"Geen GUID gevonden voor '{speler}'")
                continue

            resp = self._session.post(
                f"{BASE_URL}/Ajax/Profile/AddPlayer?id={guid}",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            if resp.status_code == 200:
                toegevoegd += 1
                self._log.info(f"Speler toegevoegd: {speler} ({guid[:8]}...)")
            else:
                self._log.warning(f"Speler '{speler}' toevoegen mislukt: {resp.status_code}")

        return toegevoegd

    def _submit_spelers(self, csrf: str):
        """Submit de spelerslijst (stap 1 -> stap 2)."""
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsPlayersPost",
            data={"__RequestVerificationToken": csrf},
            allow_redirects=True,
        )
        self._log.debug(f"ReservationsPlayersPost -> status {resp.status_code}, URL: {resp.url}")
        if resp.status_code != 200:
            raise ReserveringError(f"Spelers submiten mislukt (status {resp.status_code})")
        self._log.info(f"Spelers ingediend, naar dag-selectie (URL: {resp.url})")
        return resp

    def _selecteer_dag(self, target_date: date, tijden: list[str]) -> str:
        """Selecteer dag + dagdeel en return de HTML van de baan-pagina."""
        eerste_tijd = tijden[0] if tijden else "19:00"
        uur = int(eerste_tijd.split(":")[0])
        if uur < 12:
            dagdeel_uur = 8
        elif uur < 17:
            dagdeel_uur = 12
        else:
            dagdeel_uur = 18

        # Construeer TZ-aware datetime via constructor (niet replace()) voor DST-safety
        if isinstance(target_date, date) and not isinstance(target_date, datetime):
            dagdeel_dt = datetime(
                target_date.year, target_date.month, target_date.day,
                dagdeel_uur, 0, 0, tzinfo=NL_TZ,
            )
        else:
            dagdeel_dt = target_date.replace(
                hour=dagdeel_uur, minute=0, second=0, microsecond=0, tzinfo=NL_TZ,
            )
        utc_offset = dagdeel_dt.strftime("%z")
        tz_formatted = utc_offset[:3] + ":" + utc_offset[3:]  # "+0100" -> "+01:00"
        dagdeel_suffix = f"T{dagdeel_uur:02d}:00:00{tz_formatted}"

        selected_date = target_date.strftime("%Y-%m-%d") + dagdeel_suffix

        # Haal CSRF token van de ReservationsDay pagina
        day_page = self._session.get(f"{BASE_URL}/me/ReservationsDay")
        self._log.debug(f"ReservationsDay GET -> status {day_page.status_code}, URL: {day_page.url}")
        csrf = self._get_csrf(day_page.text)

        self._log.info(f"Selecteer dag: {selected_date}")
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsDay",
            data={
                "__RequestVerificationToken": csrf,
                "selectedDate": selected_date,
            },
            allow_redirects=True,
        )

        self._log.debug(f"ReservationsDay POST -> status {resp.status_code}, URL: {resp.url}")

        if resp.status_code != 200 or "ReservationsCourt" not in resp.url:
            self._log.error(f"Dag selectie response body (500 chars): {resp.text[:500]}")
            raise ReserveringError(
                f"Dag selectie mislukt (status {resp.status_code}, url={resp.url})"
            )

        self._log.info(f"Dag geselecteerd, op baan-pagina (URL: {resp.url})")
        return resp.text

    def _parse_beschikbare_slots(self, court_html: str) -> list[dict]:
        """Parse de baan-pagina voor beschikbare tijdslots.

        Alleen slots met EXACT class="timeincourt" (evt. met trailing whitespace)
        worden meegenomen. Slots met extra classes zoals "disabled" worden
        uitgefilterd via een negative lookahead.
        """
        slots = []
        disabled_count = 0

        court_sections = re.split(r'data-court="([a-f0-9-]+)"', court_html)
        current_court = None

        for section in court_sections:
            guid_match = re.match(r'^[a-f0-9-]+$', section.strip())
            if guid_match:
                current_court = section.strip()
                continue

            if current_court and current_court in PADEL_COURT_NAMES:
                # Tel disabled slots voor diagnostiek
                disabled_count += len(re.findall(
                    r'class="timeincourt\s+disabled', section
                ))

                # Match ALLEEN beschikbare slots: "timeincourt" gevolgd door
                # alleen whitespace tot de sluitquote (geen "disabled" e.d.)
                slot_matches = re.finditer(
                    r'class="timeincourt(?!\s+disabled)\s*"[^>]*'
                    r'data-end-time="(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}):\d{2}\+\d{2}:\d{2})"',
                    section,
                )
                for m in slot_matches:
                    end_time_full = m.group(1)
                    end_time_short = m.group(2)
                    slots.append({
                        "court_guid": current_court,
                        "court_name": PADEL_COURT_NAMES[current_court],
                        "end_time": end_time_full,
                        "end_time_short": end_time_short,
                    })

        self._log.info(
            f"Beschikbare padel-slots: {len(slots)} "
            f"(disabled/bezet gefilterd: {disabled_count})"
        )

        if len(slots) == 0 and disabled_count > 0:
            self._log.warning(
                f"0 beschikbare slots maar {disabled_count} disabled - "
                f"HTML-dump voor diagnose (eerste 1000 tekens):\n"
                f"{court_html[:1000]}"
            )

        for s in slots:
            self._log.debug(f"  {s['court_name']} end={s['end_time_short']}")
        return slots

    def _vind_beste_slot(
        self, slots: list[dict], tijden: list[str],
        baan_voorkeur: list[int], duur_min: int,
    ) -> dict | None:
        """Vind het beste beschikbare slot op basis van tijd- en baanvoorkeur.

        Controleert dat ALLE 15-minuten deelslots voor de volledige boekingsduur
        (bijv. 4 stuks voor 60 min) aaneengesloten beschikbaar zijn op dezelfde baan.
        """
        if not slots:
            return None

        benodigde_slots = duur_min // 15

        # Bouw per baan een set van beschikbare 15-min slot-starttijden
        # end_time "2026-02-25 21:30:00+01:00" → slot loopt 21:15 - 21:30
        court_slot_starts: dict[str, set[datetime]] = {}
        tz_suffix = None
        for slot in slots:
            try:
                end_dt = datetime.strptime(
                    slot["end_time"].split("+")[0].strip(),
                    "%Y-%m-%d %H:%M:%S",
                )
                start_dt = end_dt - timedelta(minutes=15)
                court_guid = slot["court_guid"]
                court_slot_starts.setdefault(court_guid, set()).add(start_dt)
                if tz_suffix is None:
                    tz_suffix = slot["end_time"][19:]
            except (ValueError, IndexError):
                continue

        if tz_suffix is None:
            tz_suffix = "+01:00"

        for gewenste_tijd in tijden:
            try:
                g_uur, g_min = map(int, gewenste_tijd.split(":"))
            except ValueError:
                continue

            for baan_nr in (baan_voorkeur or [1, 2, 3, 4]):
                court_guid = PADEL_COURTS.get(baan_nr)
                if not court_guid or court_guid not in court_slot_starts:
                    continue

                beschikbaar = court_slot_starts[court_guid]

                # Neem een willekeurige datum uit de beschikbare slots
                sample_dt = next(iter(beschikbaar))
                booking_start = sample_dt.replace(hour=g_uur, minute=g_min, second=0)

                # Controleer of ALLE 15-min deelslots beschikbaar zijn
                alle_vrij = all(
                    (booking_start + timedelta(minutes=15 * i)) in beschikbaar
                    for i in range(benodigde_slots)
                )

                if not alle_vrij:
                    vrije = sum(
                        1 for i in range(benodigde_slots)
                        if (booking_start + timedelta(minutes=15 * i)) in beschikbaar
                    )
                    self._log.debug(
                        f"Tijd {gewenste_tijd} Padel {baan_nr}: "
                        f"slechts {vrije}/{benodigde_slots} deelslots vrij"
                    )
                    continue

                eind_dt = booking_start + timedelta(minutes=duur_min)
                result = {
                    "court_guid": court_guid,
                    "court_name": PADEL_COURT_NAMES.get(court_guid, f"Padel {baan_nr}"),
                    "start_time": gewenste_tijd,
                    "start_full": booking_start.strftime("%Y-%m-%d %H:%M:%S") + tz_suffix,
                    "end_full": eind_dt.strftime("%Y-%m-%d %H:%M:%S") + tz_suffix,
                }
                self._log.info(
                    f"Beste slot: {result['start_time']} - "
                    f"{eind_dt.strftime('%H:%M')} op {result['court_name']} "
                    f"({benodigde_slots} aaneengesloten deelslots OK)"
                )
                return result

            self._log.debug(f"Tijd {gewenste_tijd} niet beschikbaar op voorkeursbanen")

        self._log.warning("Geen geschikt slot gevonden voor de volledige boekingsduur")
        return None

    def _reserveer_baan(self, court_html: str, slot: dict, dry_run: bool = False) -> tuple[bool, str]:
        """Selecteer de baan+tijd en bevestig de reservering."""
        csrf = self._get_csrf(court_html)

        self._log.info(
            f"Reserveer {slot['court_name']} van {slot['start_time']} "
            f"({slot['start_full']} - {slot['end_full']})"
        )

        if dry_run:
            return (True, f"DRY RUN - {slot['court_name']} om {slot['start_time']}")

        # Stap 3: POST court selectie
        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsCourt",
            data={
                "__RequestVerificationToken": csrf,
                "selectedCourt": slot["court_guid"],
                "selectedDate": slot["start_full"],
                "selectedEndDate": slot["end_full"],
            },
            allow_redirects=True,
        )

        self._log.debug(f"ReservationsCourt POST -> status {resp.status_code}, URL: {resp.url}")

        if resp.status_code != 200:
            return (False, f"Court selectie mislukt (status {resp.status_code})")

        # Check of we op de bevestigingspagina zijn
        if "ReservationsConfirm" in resp.url or "Confirm" in resp.url:
            return self._bevestig_reservering(resp.text)

        # Misschien is het direct bevestigd
        page_text = resp.text.lower()
        success_indicators = [
            "reservering geplaatst", "reservering bevestigd",
            "succesvol gereserveerd", "gelukt", "bevestigd",
        ]
        for indicator in success_indicators:
            if indicator in page_text:
                return (True, f"Reservering bevestigd ({indicator})")

        # Check foutmeldingen
        error_patterns = [
            ("heeft al een reservering", False),
            ("maximaal", False),
            ("bezet", True),
            ("niet beschikbaar", True),
            ("fout", True),
            ("error", True),
        ]
        for pattern, retryable in error_patterns:
            if pattern in page_text:
                return (False, f"Fout: {pattern}")

        # Als we nog op de Court pagina zijn, is er iets misgegaan
        if "ReservationsCourt" in resp.url:
            return (False, "Baan selectie niet geaccepteerd - mogelijk al bezet")

        self._log.warning(f"Onbekende status na court POST. URL: {resp.url}")
        return (False, "Onbekende status - controleer handmatig")

    def _bevestig_reservering(self, confirm_html: str) -> tuple[bool, str]:
        """Stap 4: Bevestig de reservering."""
        self._log.info("Bevestigingspagina bereikt, bevestig reservering...")

        csrf = self._get_csrf(confirm_html)

        # Zoek eventuele extra hidden fields
        extra_fields = {}
        hidden_inputs = re.findall(
            r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
            confirm_html,
        )
        for name, value in hidden_inputs:
            if name != "__RequestVerificationToken":
                extra_fields[name] = value

        post_data = {"__RequestVerificationToken": csrf, **extra_fields}

        resp = self._session.post(
            f"{BASE_URL}/me/ReservationsConfirm",
            data=post_data,
            allow_redirects=True,
        )

        self._log.debug(f"ReservationsConfirm POST -> status {resp.status_code}, URL: {resp.url}")

        page_text = resp.text.lower()

        success_indicators = [
            "reservering geplaatst", "reservering bevestigd",
            "succesvol gereserveerd", "gelukt", "bevestigd",
        ]
        for indicator in success_indicators:
            if indicator in page_text:
                self._log.info(f"Reservering bevestigd: {indicator}")
                return (True, f"Reservering bevestigd ({indicator})")

        # Check of we van de wizard-pagina's weg zijn (= succes)
        if not any(p in resp.url.lower() for p in ("reservationsplayers", "reservationsday", "reservationscourt", "reservationsconfirm")):
            self._log.info(f"Wizard verlaten (URL: {resp.url}) - reservering waarschijnlijk gelukt")
            return (True, "Reservering bevestigd (wizard verlaten)")

        # Foutmeldingen
        for pattern in ["heeft al een reservering", "maximaal", "bezet", "fout", "error"]:
            if pattern in page_text:
                return (False, f"Bevestiging mislukt: {pattern}")

        return (False, "Geen bevestiging ontvangen - controleer handmatig")

    # =========================================================================
    # SPELER DISCOVERY
    # =========================================================================

    def haal_alle_spelers(self) -> dict[str, str]:
        """
        Haal alle beschikbare clubleden op via de search API.
        Vereist een actieve sessie (eerst de spelers-pagina bezoeken).

        Returns:
            Dict van {naam: guid} voor alle beschikbare spelers.
        """
        self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
        return self._zoek_alle_spelers()

    # =========================================================================
    # PUBLIEKE INTERFACE (compatible met main.py)
    # =========================================================================

    def voorbereiden(
        self, target_date: date, tijden: list[str], spelers: list[str],
    ) -> str | None:
        """
        Bereid de reservering voor: login is al gedaan in start().
        Voeg spelers toe en submit de spelerslijst.

        Returns:
            None bij succes, foutmelding bij falen.
        """
        try:
            # Laad spelers-pagina en voeg spelers toe
            players_page = self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
            self._log.debug(
                f"ReservationsPlayers GET -> status {players_page.status_code}, "
                f"URL: {players_page.url}"
            )
            if players_page.status_code != 200:
                return f"Kon spelers-pagina niet laden (status {players_page.status_code})"

            # Detecteer redirect naar login (= sessie verlopen)
            if 'type="password"' in players_page.text.lower():
                return "Sessie verlopen - login-pagina getoond i.p.v. spelers-pagina"

            csrf = self._get_csrf(players_page.text)

            # Ontdek speler GUIDs
            self._speler_guids = self._ontdek_speler_guids()

            toegevoegd = self._voeg_spelers_toe(spelers)
            if toegevoegd == 0:
                return "Geen enkele speler kon worden toegevoegd"

            self._log.info(f"{toegevoegd}/{len(spelers)} spelers toegevoegd")

            # Submit spelers
            self._submit_spelers(csrf)
            return None

        except ReserveringError as e:
            return str(e)
        except Exception as e:
            return f"Onverwachte fout bij voorbereiding: {e}"

    def probeer_reserveer(
        self,
        target_date: date,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
        is_eerste_poging: bool = False,
    ) -> dict:
        """
        Probeer de reservering uit te voeren.

        Returns:
            Dict met: success, tijd, baan, foutmelding, retry.
        """
        result = {
            "success": False,
            "tijd": None,
            "baan": None,
            "foutmelding": None,
            "retry": False,
        }

        try:
            if not is_eerste_poging:
                # Herstart wizard: spelers opnieuw toevoegen
                fout = self.voorbereiden(target_date, tijden, spelers)
                if fout:
                    result["foutmelding"] = fout
                    result["retry"] = True
                    return result

            # Selecteer dag
            court_html = self._selecteer_dag(target_date, tijden)

            # Parse beschikbare slots
            slots = self._parse_beschikbare_slots(court_html)
            if not slots:
                self._laatste_fout = "Geen beschikbare padel-slots gevonden"
                result["foutmelding"] = self._laatste_fout
                result["retry"] = True
                return result

            # Vind beste slot
            duur = self.config.get("reservering", {}).get("duur_minuten", 60)
            slot = self._vind_beste_slot(slots, tijden, baan_voorkeur or [], duur)
            if not slot:
                beschikbare = sorted(set(s.get("end_time_short", "?") for s in slots))
                self._laatste_fout = (
                    f"Geen padelbaan vrij op gewenste tijden ({', '.join(tijden)}). "
                    f"Wel slots beschikbaar rond: {', '.join(beschikbare[:5])}"
                )
                result["foutmelding"] = self._laatste_fout
                result["retry"] = True
                return result

            result["tijd"] = slot["start_time"]
            result["baan"] = slot["court_name"]

            # Reserveer
            succes, detail = self._reserveer_baan(court_html, slot, dry_run)
            if succes:
                result["success"] = True
                result["foutmelding"] = detail if dry_run else None
            else:
                result["foutmelding"] = detail
                fout_lower = detail.lower()
                no_retry = ["heeft al een reservering", "maximaal", "niet toegestaan"]
                result["retry"] = not any(ind in fout_lower for ind in no_retry)

        except ReserveringError as e:
            result["foutmelding"] = str(e)
            result["retry"] = True
        except Exception as e:
            result["foutmelding"] = f"Fout: {e}"
            result["retry"] = True
            self._log.error(f"Fout bij reserveerpoging: {e}", exc_info=True)

        return result

    def reserveer(
        self,
        target_date: date,
        tijden: list[str],
        spelers: list[str],
        baan_voorkeur: list = None,
        dry_run: bool = False,
    ) -> dict:
        """Volledige reservering in één keer (voor lokaal testen)."""
        result = {
            "success": False,
            "datum": target_date.strftime("%d-%m-%Y"),
            "tijd": None,
            "baan": None,
            "spelers": spelers,
            "foutmelding": None,
        }

        try:
            fout = self.voorbereiden(target_date, tijden, spelers)
            if fout:
                result["foutmelding"] = fout
                return result

            poging = self.probeer_reserveer(
                target_date, tijden, spelers, baan_voorkeur, dry_run,
                is_eerste_poging=True,
            )
            result["success"] = poging["success"]
            result["tijd"] = poging["tijd"]
            result["baan"] = poging["baan"]
            result["foutmelding"] = poging["foutmelding"]

        except Exception as e:
            result["foutmelding"] = f"Onverwachte fout: {e}"
            self._log.error(f"Onverwachte fout: {e}", exc_info=True)

        return result
