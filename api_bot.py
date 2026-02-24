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

import logging
import os
import re
from datetime import datetime, timedelta

import requests

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

        if resp.status_code != 200 or "input[type='password']" in resp.text.lower():
            raise ReserveringError(f"Login mislukt (status {resp.status_code})")

        self._log.info(f"Login geslaagd ({len(self._session.cookies)} cookies)")

    def _get_csrf(self, html: str) -> str:
        match = re.search(r'__RequestVerificationToken.*?value="([^"]+)"', html)
        if not match:
            raise ReserveringError("CSRF token niet gevonden")
        return match.group(1)

    def _ontdek_speler_guids(self) -> dict[str, str]:
        """Parse de ReservationsPlayers pagina om speler GUIDs te vinden."""
        resp = self._session.get(f"{BASE_URL}/me/ReservationsPlayers")
        if resp.status_code != 200:
            raise ReserveringError(f"Kon spelers-pagina niet laden (status {resp.status_code})")

        guids = {}
        # Zoek patronen: naam gevolgd door of voorafgegaan door een AddPlayer GUID
        # De pagina toont spelers met hun naam en een knop met het GUID
        blocks = re.findall(
            r'AddPlayer[^"]*id=([a-f0-9-]+)[^>]*>.*?</.*?'
            r'|'
            r'([A-Z][a-zà-ü]+(?: [a-zà-ü]+)*(?: [A-Z][a-zà-ü]+)+)',
            resp.text, re.DOTALL
        )

        # Alternatief: zoek GUID-naam paren via de pagina-structuur
        # Elke spelerkaart bevat een GUID in de URL en een naam in de tekst
        all_guids = re.findall(r'AddPlayer[^"]*id=([a-f0-9-]+)', resp.text)
        all_names = []
        for guid in all_guids:
            idx = resp.text.find(guid)
            context = resp.text[max(0, idx-500):idx+500]
            clean = re.sub(r'<[^>]+>', ' ', context)
            names = re.findall(r'([A-Z][a-zà-ü-]+(?: (?:van |de |den |der )?[A-Za-zà-ü-]+)+)', clean)
            for name in names:
                name = name.strip()
                if len(name) > 4 and name not in guids:
                    guids[name] = guid
                    break

        if not guids:
            # Fallback: alle GUIDs op de pagina extraheren
            self._log.warning("Kon speler-namen niet koppelen aan GUIDs, gebruik bekende mapping")

        self._log.info(f"Speler GUIDs gevonden: {len(guids)}")
        for naam, guid in guids.items():
            self._log.debug(f"  {naam} = {guid}")

        return guids

    def _voeg_spelers_toe(self, spelers: list[str]) -> int:
        """Voeg spelers toe via AJAX en return het aantal succesvol toegevoegde."""
        if not self._speler_guids:
            self._speler_guids = self._ontdek_speler_guids()

        toegevoegd = 0
        for speler in spelers:
            guid = self._speler_guids.get(speler)
            if not guid:
                # Zoek op achternaam
                achternaam = speler.split()[-1].lower()
                for naam, g in self._speler_guids.items():
                    if achternaam in naam.lower():
                        guid = g
                        break

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
        if resp.status_code != 200:
            raise ReserveringError(f"Spelers submiten mislukt (status {resp.status_code})")
        self._log.info("Spelers ingediend, naar dag-selectie")
        return resp

    def _selecteer_dag(self, target_date: datetime, tijden: list[str]) -> str:
        """Selecteer dag + dagdeel en return de HTML van de baan-pagina."""
        eerste_tijd = tijden[0] if tijden else "19:00"
        uur = int(eerste_tijd.split(":")[0])
        if uur < 12:
            dagdeel_suffix = "T08:00:00Z"
        elif uur < 17:
            dagdeel_suffix = "T12:00:00Z"
        else:
            dagdeel_suffix = "T18:00:00Z"

        selected_date = target_date.strftime(f"%Y-%m-%d") + dagdeel_suffix

        # Haal CSRF token van de ReservationsDay pagina
        day_page = self._session.get(f"{BASE_URL}/me/ReservationsDay")
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

        if resp.status_code != 200 or "ReservationsCourt" not in resp.url:
            raise ReserveringError(
                f"Dag selectie mislukt (status {resp.status_code}, url={resp.url})"
            )

        self._log.info("Dag geselecteerd, op baan-pagina")
        return resp.text

    def _parse_beschikbare_slots(self, court_html: str) -> list[dict]:
        """Parse de baan-pagina voor beschikbare tijdslots."""
        slots = []
        # Zoek elementen met data-court en data-end-time (beschikbare slots)
        # Beschikbare slots hebben class "timeincourt" zonder "disabled"
        # We zoeken op data-court + data-end-time combinaties
        pattern = re.compile(
            r'data-court="([a-f0-9-]+)".*?'
            r'class="timeincourt\s*".*?'  # Geen "disabled"
            r'data-end-time="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\+\d{2}:\d{2})"',
            re.DOTALL,
        )

        # Alternatieve aanpak: zoek per court-sectie
        court_sections = re.split(r'data-court="([a-f0-9-]+)"', court_html)
        current_court = None

        for i, section in enumerate(court_sections):
            guid_match = re.match(r'^[a-f0-9-]+$', section.strip())
            if guid_match:
                current_court = section.strip()
                continue

            if current_court and current_court in PADEL_COURT_NAMES:
                # Zoek beschikbare (niet-disabled) slots in deze sectie
                # Beschikbare slots: class="timeincourt  " (zonder disabled)
                # Met data-end-time
                slot_matches = re.finditer(
                    r'class="timeincourt\s+"[^>]*'
                    r'data-end-time="(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}):\d{2}\+\d{2}:\d{2})"',
                    section,
                )
                for m in slot_matches:
                    end_time_full = m.group(1)
                    end_time_short = m.group(2)
                    # Bereken starttijd (end_time - 15 min want slots zijn per 15 min)
                    slots.append({
                        "court_guid": current_court,
                        "court_name": PADEL_COURT_NAMES[current_court],
                        "end_time": end_time_full,
                        "end_time_short": end_time_short,
                    })

        # Groepeer slots per court en bepaal beschikbare starttijden
        # Een slot met end_time "20:30" en duur 60 min = start om 19:30
        self._log.info(f"Gevonden beschikbare padel-slots: {len(slots)}")
        return slots

    def _vind_beste_slot(
        self, slots: list[dict], tijden: list[str],
        baan_voorkeur: list[int], duur_min: int,
    ) -> dict | None:
        """Vind het beste beschikbare slot op basis van tijd- en baanvoorkeur."""
        if not slots:
            return None

        # Bouw een set van beschikbare starttijden per court
        # end_time "2026-02-25 21:30:00+01:00" met duur 60 = start "20:30"
        start_slots = []
        for slot in slots:
            try:
                end_dt = datetime.strptime(
                    slot["end_time"].split("+")[0].strip(),
                    "%Y-%m-%d %H:%M:%S",
                )
                start_dt = end_dt - timedelta(minutes=15)  # Elk slot is 15 min
                start_time_str = start_dt.strftime("%H:%M")

                # Bereken het volledige tijdslot (startuur:minuut) dat we willen matchen
                start_slots.append({
                    **slot,
                    "start_dt": start_dt,
                    "start_time": start_time_str,
                    "start_full": start_dt.strftime("%Y-%m-%d %H:%M:%S") + slot["end_time"][19:],
                })
            except (ValueError, IndexError):
                continue

        # Probeer elke voorkeurstijd
        for gewenste_tijd in tijden:
            # Zoek slots die beginnen op deze tijd
            matching = [s for s in start_slots if s["start_time"] == gewenste_tijd]
            if not matching:
                self._log.debug(f"Tijd {gewenste_tijd} niet beschikbaar")
                continue

            # Check of er genoeg aaneengesloten slots zijn voor de gewenste duur
            for baan_nr in (baan_voorkeur or [1, 2, 3, 4]):
                court_guid = PADEL_COURTS.get(baan_nr)
                if not court_guid:
                    continue

                baan_slots = [s for s in matching if s["court_guid"] == court_guid]
                if baan_slots:
                    slot = baan_slots[0]
                    # Bereken eindtijd op basis van duur
                    eind_dt = slot["start_dt"] + timedelta(minutes=duur_min)
                    tz_suffix = slot["end_time"][19:]  # bijv. "+01:00"
                    result = {
                        "court_guid": court_guid,
                        "court_name": PADEL_COURT_NAMES.get(court_guid, f"Padel {baan_nr}"),
                        "start_time": gewenste_tijd,
                        "start_full": slot["start_dt"].strftime("%Y-%m-%d %H:%M:%S") + tz_suffix,
                        "end_full": eind_dt.strftime("%Y-%m-%d %H:%M:%S") + tz_suffix,
                    }
                    self._log.info(
                        f"Beste slot: {result['start_time']} op {result['court_name']}"
                    )
                    return result

            self._log.debug(f"Tijd {gewenste_tijd} niet beschikbaar op voorkeursbanen")

        self._log.warning("Geen geschikt slot gevonden")
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
    # PUBLIEKE INTERFACE (compatible met main.py)
    # =========================================================================

    def voorbereiden(
        self, target_date: datetime, tijden: list[str], spelers: list[str],
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
            if players_page.status_code != 200:
                return f"Kon spelers-pagina niet laden (status {players_page.status_code})"

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
        target_date: datetime,
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
        target_date: datetime,
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
